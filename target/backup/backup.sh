#!/bin/sh
# Periodic backup of the target database.
# Each cycle copies app.db to a temporary file, validates the copy with
# scripts/find_known_good_backup.py --check (integrity ok, widgets has the columns the
# target reads), and only then renames it to app-<epoch>.db. Faulty states (corrupt file,
# dropped or mis-shaped table) are therefore never kept as backups, and a partially
# written copy never carries a backup name. Only the newest BACKUP_RETENTION copies are kept.
set -eu

DB_FILE="${BACKUP_DB_FILE:-/target/app/data/app.db}"
BACKUP_DIR="${BACKUP_DIR:-/target/backups}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-10}"
RETENTION="${BACKUP_RETENTION:-360}"
PYTHON="${BACKUP_PYTHON:-python3}"
VALIDATOR="${BACKUP_VALIDATOR:-/scripts/find_known_good_backup.py}"

mkdir -p "$BACKUP_DIR"

backup_once() {
  [ -f "$DB_FILE" ] || return 0
  ts=$(date +%s)
  tmp="$BACKUP_DIR/.app-$ts.db.tmp"
  cp "$DB_FILE" "$tmp"
  if "$PYTHON" "$VALIDATOR" --check "$tmp"; then
    mv "$tmp" "$BACKUP_DIR/app-$ts.db"
  else
    rm -f "$tmp"
    echo "backup skipped: database failed validation" >&2
  fi
  # Retention: keep the newest $RETENTION backups. Names are always app-<digits>.db.
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/app-*.db 2>/dev/null | tail -n +"$((RETENTION + 1))" | while read -r old; do
    rm -f "$old"
  done
}

while true; do
  backup_once || echo "backup cycle failed" >&2
  [ "${BACKUP_ONCE:-0}" = "1" ] && break
  sleep "$INTERVAL"
done
