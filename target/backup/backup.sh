#!/bin/sh
set -eu

DB_FILE="/target/app/data/app.db"
BACKUP_DIR="/target/backups"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-10}"

mkdir -p "$BACKUP_DIR"

while true; do
  if [ -f "$DB_FILE" ]; then
    ts=$(date +%s)
    cp "$DB_FILE" "$BACKUP_DIR/app-$ts.db"
  fi
  sleep "$INTERVAL"
done
