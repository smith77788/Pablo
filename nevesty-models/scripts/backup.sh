#!/bin/bash
# SQLite backup script for Nevesty Models
# Runs every 6 hours via scheduler.js; keeps last 28 backups (7 days × 4/day)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
DB_PATH="${DB_PATH:-$APP_DIR/data.db}"
FACTORY_DB="$APP_DIR/../factory/factory.db"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
MAX_BACKUPS="${MAX_BACKUPS:-28}"  # 7 days × 4 backups/day

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y-%m-%d_%H-%M)

# Backup main application DB
if [ -f "$DB_PATH" ]; then
  DEST="$BACKUP_DIR/nevesty_${TIMESTAMP}.db"
  if command -v sqlite3 &>/dev/null; then
    sqlite3 "$DB_PATH" ".backup '$DEST'"
  else
    cp "$DB_PATH" "$DEST"
  fi
  echo "[backup] data.db → nevesty_${TIMESTAMP}.db"
fi

# Backup factory DB (if present)
if [ -f "$FACTORY_DB" ]; then
  FACTORY_DEST="$BACKUP_DIR/factory_${TIMESTAMP}.db"
  if command -v sqlite3 &>/dev/null; then
    sqlite3 "$FACTORY_DB" ".backup '$FACTORY_DEST'"
  else
    cp "$FACTORY_DB" "$FACTORY_DEST"
  fi
  echo "[backup] factory.db → factory_${TIMESTAMP}.db"
fi

# Keep only last MAX_BACKUPS of each type
for prefix in nevesty factory; do
  mapfile -t old_backups < <(ls -t "$BACKUP_DIR/${prefix}_"*.db 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)))
  if [ ${#old_backups[@]} -gt 0 ]; then
    rm -- "${old_backups[@]}"
    echo "[backup] Removed ${#old_backups[@]} old ${prefix} backup(s)"
  fi
done

echo "[backup] Done. Backups in $BACKUP_DIR"
