#!/bin/bash
# Restore SQLite database from backup
# Usage: ./scripts/restore-db.sh /app/backups/nevesty_2026-05-15_06-00.db
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP="$1"
DB_PATH="${DB_PATH:-$APP_DIR/data.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"

if [ -z "$BACKUP" ]; then
  echo "Usage: $0 <backup_file.db>"
  echo ""
  echo "Available backups:"
  ls -lt "$BACKUP_DIR"/nevesty_*.db 2>/dev/null | awk '{print $NF}' | head -20 || echo "  No backups found in $BACKUP_DIR"
  exit 1
fi

if [ ! -f "$BACKUP" ]; then
  echo "Error: backup file not found: $BACKUP"
  exit 1
fi

echo "This will replace $DB_PATH with $BACKUP"
read -r -p "Continue? (y/N) " confirm
if [ "$confirm" != "y" ]; then
  echo "Cancelled."
  exit 0
fi

# Create a safety snapshot before restoring
SAFETY_COPY="${DB_PATH}.pre-restore.$(date +%s)"
echo "Creating safety copy: $SAFETY_COPY"
cp "$DB_PATH" "$SAFETY_COPY"

echo "Restoring from: $BACKUP"
cp "$BACKUP" "$DB_PATH"
echo "Restore complete. Restart the application."
echo "Safety copy kept at: $SAFETY_COPY"
