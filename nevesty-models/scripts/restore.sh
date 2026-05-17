#!/bin/bash
BACKUP=$1
DB=${DB_PATH:-nevesty.db}
if [ -z "$BACKUP" ]; then echo "Usage: $0 <backup-file>"; exit 1; fi
if [ ! -f "$BACKUP" ]; then echo "Backup not found: $BACKUP"; exit 1; fi
cp "$DB" "${DB}.restore-backup-$(date +%s)"
cp "$BACKUP" "$DB"
echo "✅ Restored from $BACKUP"
