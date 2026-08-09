#!/bin/bash
# Daily PostgreSQL backup for HCC (Hanyan Cognitive Core).
#
# Restore with:
#   pg_restore -h localhost -U hcc -d hcc --clean --if-exists <file>
set -euo pipefail

# launchd runs with a minimal PATH that does NOT include Homebrew, so a bare
# `pg_dump` resolved to "command not found" (exit 127) and the pipe wrote a
# 20-byte gzip-of-nothing every night since 2026-08-07. Pin Homebrew's bin dir
# on PATH so pg_dump/gzip resolve under launchd exactly as in an interactive
# shell.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

BACKUP_DIR="${HCC_BACKUP_DIR:-$HOME/backups/hcc}"
FILE="hcc-$(date +%Y%m%d-%H%M).sql.gz"
LOG="$BACKUP_DIR/backup.log"
RETAIN=14

mkdir -p "$BACKUP_DIR"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting backup -> $FILE"
  PGPASSWORD=hcc pg_dump -h localhost -U hcc -d hcc -Fc | gzip > "$BACKUP_DIR/$FILE"
  # Guard against silent empty backups: a healthy -Fc dump of this DB is tens of
  # MB, so anything under 100KB means pg_dump produced nothing — fail loudly
  # (non-zero exit → launchd logs it) instead of retaining a useless file.
  _size=$(stat -f%z "$BACKUP_DIR/$FILE")
  if [ "$_size" -lt 102400 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: backup only ${_size} bytes — pg_dump likely failed; removing"
    rm -f "$BACKUP_DIR/$FILE"
    exit 1
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup complete: ${_size} bytes"

  cd "$BACKUP_DIR"
  ls -1t hcc-*.sql.gz | tail -n "+$((RETAIN + 1))" | xargs -r rm -f
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] retention: keeping newest $RETAIN backups"
} >> "$LOG" 2>&1
