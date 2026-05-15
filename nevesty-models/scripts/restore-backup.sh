#!/bin/bash
# Usage: ./scripts/restore-backup.sh backup/nevesty_2026-05-15T08-00-00.db
set -e

BACKUP_FILE=$1
DB_PATH=${DB_PATH:-"./data/nevesty.db"}
BACKUP_DIR=${BACKUP_DIR:-"./backups"}

if [ -z "$BACKUP_FILE" ]; then
  echo "Usage: $0 <backup-file>"
  echo ""
  echo "Available backups:"
  ls "$BACKUP_DIR"/nevesty_*.db 2>/dev/null | sort -r | head -10 || echo "  (none found in $BACKUP_DIR)"
  exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
  echo "Error: Backup file not found: $BACKUP_FILE"
  exit 1
fi

echo "⚠️  This will replace $DB_PATH with $BACKUP_FILE"
read -p "Continue? (y/N) " confirm
if [ "$confirm" != "y" ]; then
  echo "Cancelled."
  exit 0
fi

echo "Creating safety copy..."
cp "$DB_PATH" "${DB_PATH}.before-restore-$(date +%Y%m%d%H%M%S)"

echo "Restoring from $BACKUP_FILE..."
cp "$BACKUP_FILE" "$DB_PATH"
echo "✅ Restore complete. Restart the application."
