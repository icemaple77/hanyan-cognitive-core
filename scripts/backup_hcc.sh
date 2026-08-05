#!/bin/bash
# Daily PostgreSQL backup for HCC (Hanyan Cognitive Core).
#
# Restore with:
#   pg_restore -h localhost -U hcc -d hcc --clean --if-exists <file>
set -euo pipefail

BACKUP_DIR="${HCC_BACKUP_DIR:-$HOME/backups/hcc}"
FILE="hcc-$(date +%Y%m%d-%H%M).sql.gz"
LOG="$BACKUP_DIR/backup.log"
RETAIN=14

mkdir -p "$BACKUP_DIR"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting backup -> $FILE"
  PGPASSWORD=hcc pg_dump -h localhost -U hcc -d hcc -Fc | gzip > "$BACKUP_DIR/$FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup complete: $(ls -la "$BACKUP_DIR/$FILE" | awk '{print $5}') bytes"

  cd "$BACKUP_DIR"
  ls -1t hcc-*.sql.gz | tail -n "+$((RETAIN + 1))" | xargs -r rm -f
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] retention: keeping newest $RETAIN backups"
} >> "$LOG" 2>&1
