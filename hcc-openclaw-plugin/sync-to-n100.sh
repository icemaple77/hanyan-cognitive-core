#!/bin/bash
# Sync the hcc-openclaw-plugin from macmini (authoritative source) to N100,
# where OpenClaw actually loads it.
#
# Why this exists (2026-08-09 排查 P2-5): the plugin has two copies — the source
# of truth lives here on macmini (this repo), and N100 runs a plain file copy at
# ~/hcc-openclaw-plugin. Edits on macmini used to be rsync'd by hand, which is
# easy to forget after a fix. Run this after every plugin change.
#
#   ./sync-to-n100.sh            # preview diff, then sync (asks before restart)
#   ./sync-to-n100.sh --restart  # sync AND restart OpenClaw on N100 (needs sudo)
#   ./sync-to-n100.sh --dry-run  # show what would change, do nothing
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)/"
N100_HOST="${N100_HOST:-n100}"
N100_DIR="${N100_DIR:-/home/michael/hcc-openclaw-plugin/}"

# Only the runtime files OpenClaw needs — never push node_modules, .git, backups.
INCLUDES=(index.js package.json openclaw.plugin.json README.md)

DRY_RUN=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --restart) RESTART=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

RSYNC_FLAGS=(-avz --checksum)
FILTERS=()
for f in "${INCLUDES[@]}"; do FILTERS+=(--include="$f"); done
FILTERS+=(--exclude='*')

echo "=== diff preview (macmini → $N100_HOST) ==="
rsync "${RSYNC_FLAGS[@]}" --dry-run --itemize-changes "${FILTERS[@]}" \
  "$SRC_DIR" "$N100_HOST:$N100_DIR" || true

if [ "$DRY_RUN" -eq 1 ]; then
  echo "(dry-run) nothing written."
  exit 0
fi

echo "=== syncing ==="
rsync "${RSYNC_FLAGS[@]}" "${FILTERS[@]}" "$SRC_DIR" "$N100_HOST:$N100_DIR"

echo "=== verifying index.js checksum ==="
LOCAL_SHA=$(shasum "${SRC_DIR}index.js" | awk '{print $1}')
REMOTE_SHA=$(ssh "$N100_HOST" "shasum ${N100_DIR}index.js" | awk '{print $1}')
if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  echo "✅ in sync: $LOCAL_SHA"
else
  echo "❌ checksum mismatch — local=$LOCAL_SHA remote=$REMOTE_SHA" >&2
  exit 1
fi

if [ "$RESTART" -eq 1 ]; then
  echo "=== restarting OpenClaw on $N100_HOST (sudo) ==="
  ssh -t "$N100_HOST" 'sudo systemctl restart openclaw-gateway.service && systemctl is-active openclaw-gateway.service'
else
  echo
  echo "Plugin synced. To activate on N100, restart OpenClaw:"
  echo "  ssh $N100_HOST 'sudo systemctl restart openclaw-gateway.service'"
  echo "(or re-run with --restart)"
fi
