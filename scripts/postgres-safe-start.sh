#!/bin/bash
# postgres-safe-start.sh — launchd 包装脚本,启动 postgres 前自愈僵尸锁文件。
#
# 2026-08-24 深夜:内核崩溃(kernel panic)硬重启后,postgres 留下的
# postmaster.pid 锁文件里记的旧 PID 被系统重启后完全无关的另一个进程复用
# (当晚是 SiriAUSP),postgres 一看"这个 PID 活着"就拒绝启动,HCC(全 agent
# 记忆命根)跟着起不来,得人工确认+手动删锁文件才能救回。这脚本把那次人工
# 排查的判断逻辑自动化,正常情况(锁文件不存在,或锁文件对应的确实是活着的
# postgres)完全不干预,只有确认是"僵尸锁"(PID 不存在,或存在但不是
# postgres 进程——避免误删真锁)才清。
set -euo pipefail

PGDATA="/opt/homebrew/var/postgresql@17"
LOCKFILE="$PGDATA/postmaster.pid"
POSTGRES_BIN="/opt/homebrew/opt/postgresql@17/bin/postgres"
LOG="/opt/homebrew/var/log/postgresql@17-safestart.log"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*" >>"$LOG"; }

if [ -f "$LOCKFILE" ]; then
  old_pid="$(head -1 "$LOCKFILE" 2>/dev/null | tr -d '[:space:]')"
  if [ -n "$old_pid" ]; then
    # 只有"PID 不存在" 或 "PID 存在但命令名不是 postgres" 才判定为僵尸锁——
    # 后者正是那晚的真实场景(PID 被系统重启后的无关进程复用)。
    comm="$(ps -p "$old_pid" -o comm= 2>/dev/null || true)"
    if [ -z "$comm" ]; then
      log "锁文件 PID $old_pid 已不存在,判定僵尸锁,清除"
      rm -f "$LOCKFILE"
    elif [[ "$comm" != *postgres* ]]; then
      log "锁文件 PID $old_pid 活着但不是 postgres($comm),判定僵尸锁(PID 被复用),清除"
      rm -f "$LOCKFILE"
    else
      log "锁文件 PID $old_pid 确实是活着的 postgres,不动它"
    fi
  fi
fi

exec "$POSTGRES_BIN" -D "$PGDATA"
