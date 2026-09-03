#!/usr/bin/env bash
# task_driver.sh — Task-Schedule last-mile cron driver (看板卡 t_6b29b140).
#
# The external glue that makes the agent anti-stall loop actually fire without a
# human in the seat. On each cron tick it:
#   1. polls HCC for this runtime's due long-tasks   (GET /tasks/due?agent_id=…)
#   2. per due task, wakes marching orders           (POST /tasks/{id}/wake)
#   3. action=="work"     → spawn a fresh UNATTENDED session on the prompt; that
#                           session runs the step's verify_cmd, pushes the step
#                           forward, and reports back (task_report) on its own.
#      action=="escalate" → do NOT spawn work; notify the human (HCC_NOTIFY_CMD)
#                           and/or log — the task is already BLOCKED server-side.
#      action=="none"     → task no longer running; skip.
#
# The wake sets a server-side lease (next_wake_at += est), so a task already
# being worked is not re-listed as due until the lease lapses — this driver does
# not need to track in-flight sessions itself, though it also drops a short-lived
# marker as belt-and-suspenders.
#
# Determinism: this driver never judges progress. Only the woken session's
# verify_cmd + task_report moves the state machine.
#
# Env knobs (all optional):
#   HCC_AGENT_ID     runtime to drive           (default: hanyan; or $1)
#   HCC_BASE         gateway API base           (default: http://localhost:8000/api/v1)
#   HCC_PROJECT_DIR  cwd for spawned sessions   (default: this repo)
#   HCC_DUE_LIMIT    max due tasks per tick     (default: 20)
#   HCC_TASK_LOG_DIR log dir                    (default: ~/.hcc/task-driver)
#   HCC_SPAWN_CMD    override spawn; receives prompt on stdin (default: claude -p headless)
#   HCC_NOTIFY_CMD   escalation sink; receives prompt on stdin (default: log only)
#   DRIVER_DRYRUN=1  print what would be spawned/escalated, change nothing external
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

AGENT_ID="${1:-${HCC_AGENT_ID:-hanyan}}"
HCC_BASE="${HCC_BASE:-http://localhost:8000/api/v1}"
PROJECT_DIR="${HCC_PROJECT_DIR:-$REPO_DIR}"
LIMIT="${HCC_DUE_LIMIT:-20}"
LOG_DIR="${HCC_TASK_LOG_DIR:-$HOME/.hcc/task-driver}"
DRYRUN="${DRIVER_DRYRUN:-0}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/driver.log"
log() { printf '%s [%s] %s\n' "$(date -Iseconds)" "$AGENT_ID" "$*" >>"$LOG"; }

# ── single-flight (portable, no flock): atomic mkdir lock, steal if stale >30m ──
LOCKDIR="${HCC_TASK_LOCK:-/tmp/hcc-task-driver-$AGENT_ID.lock}"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # stale-lock recovery: if the lock dir is older than 30 min, a prior run died.
  if [ -d "$LOCKDIR" ] && [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    log "stealing stale lock $LOCKDIR"
    rmdir "$LOCKDIR" 2>/dev/null || true
    mkdir "$LOCKDIR" 2>/dev/null || { log "lock race, skipping tick"; exit 0; }
  else
    log "another driver run holds the lock, skipping tick"
    exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# ── default spawn: headless, unattended Claude Code in the project dir ──────────
# Unattended work needs to run bash (verify_cmd) and edit/act without a human to
# approve prompts, so the default skips permission gating. The server-side redline
# gate already prevents dangerous steps (删除/花钱/对外/家庭域) from ever reaching a
# "work" payload — those escalate instead. Override HCC_SPAWN_CMD to tighten this
# (e.g. --allowedTools) or to plug in hermes/openclaw's own session-spawn.
default_spawn() {  # prompt on stdin
  local prompt; prompt="$(cat)"
  ( cd "$PROJECT_DIR" && claude -p "$prompt" \
      --output-format text \
      --dangerously-skip-permissions ) \
    >>"$LOG_DIR/spawn-$AGENT_ID.log" 2>&1 &
  log "spawned work session pid=$! (detached)"
}

default_notify() {  # escalation prompt on stdin
  local msg; msg="$(cat)"
  printf '%s [%s] ESCALATE\n%s\n' "$(date -Iseconds)" "$AGENT_ID" "$msg" >>"$LOG_DIR/escalations.log"
  log "escalation logged (set HCC_NOTIFY_CMD to route to 飞书/微信)"
}

spawn_work()  { if [ -n "${HCC_SPAWN_CMD:-}" ]; then "$SHELL" -c "$HCC_SPAWN_CMD"; else default_spawn;  fi; }
notify_human(){ if [ -n "${HCC_NOTIFY_CMD:-}" ]; then "$SHELL" -c "$HCC_NOTIFY_CMD"; else default_notify; fi; }

# ── poll due ────────────────────────────────────────────────────────────────
due_json="$(curl -s -m 10 "$HCC_BASE/tasks/due?agent_id=$AGENT_ID&limit=$LIMIT" || true)"
if [ -z "$due_json" ]; then log "gateway unreachable at $HCC_BASE, skipping tick"; exit 0; fi
count="$(printf '%s' "$due_json" | jq -r '.count // 0')"
log "due=$count"
[ "$count" = "0" ] && exit 0

# ── per due task: wake → dispatch ─────────────────────────────────────────────
printf '%s' "$due_json" | jq -r '.tasks[].id' | while read -r tid; do
  [ -z "$tid" ] && continue
  wake="$(curl -s -m 10 -X POST "$HCC_BASE/tasks/$tid/wake" || true)"
  if [ -z "$wake" ]; then log "$tid: wake failed"; continue; fi
  action="$(printf '%s' "$wake" | jq -r '.action // "none"')"
  prompt="$(printf '%s' "$wake" | jq -r '.prompt // ""')"
  title="$(printf '%s'  "$wake" | jq -r '.title // ""')"

  case "$action" in
    work)
      if [ "$DRYRUN" = "1" ]; then
        log "DRYRUN would spawn work for $tid「$title」"
        printf -- '--- DRYRUN work %s ---\n%s\n' "$tid" "$prompt"
      else
        log "$tid「$title」→ work"
        printf '%s' "$prompt" | spawn_work
      fi
      ;;
    escalate)
      if [ "$DRYRUN" = "1" ]; then
        log "DRYRUN would escalate $tid「$title」"
        printf -- '--- DRYRUN escalate %s ---\n%s\n' "$tid" "$prompt"
      else
        log "$tid「$title」→ escalate"
        printf '%s' "$prompt" | notify_human
      fi
      ;;
    *)
      log "$tid → $action (skip)"
      ;;
  esac
done

log "tick done"
