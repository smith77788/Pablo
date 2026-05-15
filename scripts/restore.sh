#!/bin/bash
# Restore a SQLite database backup for Nevesty Models
#
# Usage:
#   ./restore.sh data_20240101_120000.db          — restore nevesty-models DB
#   ./restore.sh factory_20240101_120000.db       — restore factory DB
#   ./restore.sh data_20240101_120000.db /custom/target.db  — restore to custom path
#
set -e

BACKUP_DIR="/home/user/Pablo/backups"
NEVESTY_DB="/home/user/Pablo/nevesty-models/data.db"
FACTORY_DB="/home/user/Pablo/factory/factory.db"

if [ -z "$1" ]; then
  echo "Usage: $0 <backup_filename> [target_path]"
  echo ""
  echo "Available backups:"
  ls -lh "$BACKUP_DIR"/*.db 2>/dev/null || echo "  No backups found in $BACKUP_DIR"
  exit 1
fi

BACKUP_FILE="$BACKUP_DIR/$1"

# If the argument is already an absolute path, use it directly
if [[ "$1" == /* ]]; then
  BACKUP_FILE="$1"
fi

if [ ! -f "$BACKUP_FILE" ]; then
  echo "ERROR: Backup file not found: $BACKUP_FILE"
  exit 1
fi

# Determine target path
if [ -n "$2" ]; then
  TARGET="$2"
elif [[ "$1" == factory_* ]]; then
  TARGET="$FACTORY_DB"
else
  TARGET="$NEVESTY_DB"
fi

# Safety: confirm before overwriting
echo "This will overwrite: $TARGET"
echo "With backup:         $BACKUP_FILE"
read -r -p "Continue? [y/N] " confirm
case "$confirm" in
  [yY][eE][sS]|[yY])
    ;;
  *)
    echo "Restore cancelled."
    exit 0
    ;;
esac

# Create a timestamped pre-restore backup of the current DB
PRE_DATE=$(date +%Y%m%d_%H%M%S)
PRE_BACKUP="$BACKUP_DIR/pre_restore_$(basename "$TARGET" .db)_$PRE_DATE.db"
if [ -f "$TARGET" ]; then
  cp "$TARGET" "$PRE_BACKUP"
  echo "Pre-restore backup saved: $PRE_BACKUP"
fi

# Stop WAL/SHM files to avoid corruption (best-effort)
for ext in -shm -wal; do
  [ -f "${TARGET}${ext}" ] && rm -f "${TARGET}${ext}" && echo "Removed ${TARGET}${ext}"
done

cp "$BACKUP_FILE" "$TARGET"
echo "Restore completed: $(basename "$BACKUP_FILE") -> $TARGET"
