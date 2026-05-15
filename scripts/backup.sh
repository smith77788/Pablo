#!/bin/bash
set -euo pipefail

BACKUP_DIR="/home/user/Pablo/backups"
DATA_DIR="/home/user/Pablo/nevesty-models"
DB_FILE="$DATA_DIR/data.db"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/nevesty_models_${TIMESTAMP}.db"
LOG_FILE="$BACKUP_DIR/backup.log"
KEEP_DAYS=7

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting backup..." >> "$LOG_FILE"

# Check if DB exists
if [ ! -f "$DB_FILE" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: DB not found at $DB_FILE" >> "$LOG_FILE"
  exit 1
fi

# SQLite online backup (safe while DB is in use)
sqlite3 "$DB_FILE" ".backup '$BACKUP_FILE'"
BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup created: $BACKUP_FILE ($BACKUP_SIZE)" >> "$LOG_FILE"

# Compress backup
gzip -f "$BACKUP_FILE"
COMPRESSED="${BACKUP_FILE}.gz"
COMPRESSED_SIZE=$(du -h "$COMPRESSED" | cut -f1)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Compressed: $COMPRESSED ($COMPRESSED_SIZE)" >> "$LOG_FILE"

# Remove old backups (older than KEEP_DAYS days)
OLD_COUNT=$(find "$BACKUP_DIR" -name "*.db.gz" -mtime +$KEEP_DAYS | wc -l)
find "$BACKUP_DIR" -name "*.db.gz" -mtime +$KEEP_DAYS -delete
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Removed $OLD_COUNT old backups" >> "$LOG_FILE"

# Show stats
TOTAL=$(find "$BACKUP_DIR" -name "*.db.gz" | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done. Total backups: $TOTAL" >> "$LOG_FILE"
