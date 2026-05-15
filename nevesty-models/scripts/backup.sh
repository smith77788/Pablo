#!/bin/bash
# Database backup script — run via cron or manually
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
DB_PATH="$APP_DIR/data.db"
FACTORY_DB="$APP_DIR/../factory/factory.db"
MAX_BACKUPS="${MAX_BACKUPS:-14}"  # keep 2 weeks

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Backup main DB
if [ -f "$DB_PATH" ]; then
  sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/data_${TIMESTAMP}.db'"
  echo "[backup] data.db → data_${TIMESTAMP}.db"
fi

# Backup factory DB
if [ -f "$FACTORY_DB" ]; then
  sqlite3 "$FACTORY_DB" ".backup '$BACKUP_DIR/factory_${TIMESTAMP}.db'"
  echo "[backup] factory.db → factory_${TIMESTAMP}.db"
fi

# Remove old backups (keep MAX_BACKUPS most recent per type)
for prefix in data factory; do
  ls -t "$BACKUP_DIR/${prefix}_"*.db 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm --
done

echo "[backup] Done. Backups in $BACKUP_DIR"
