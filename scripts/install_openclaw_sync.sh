#!/usr/bin/env bash
# Installs the periodic OpenClaw->HCC memory sync launchd job (P2-10).
# NOT run automatically by anything — a human runs this deliberately.
#
# Before running: read scripts/com.hanyan.hcc-openclaw-sync.plist's header
# comment and confirm this doesn't duplicate ~/.openclaw/scripts/memory-bridge.js
# on the N100 host, which was not reachable/inspectable from this session.
set -euo pipefail

PLIST_NAME="com.hanyan.hcc-openclaw-sync.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/$PLIST_NAME"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/.hcc/openclaw-sync"
cp "$SRC" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "installed + loaded: $DEST"
echo "logs: $HOME/.hcc/openclaw-sync/launchd.{out,err}.log"
echo "to stop: launchctl unload $DEST"
