#!/bin/bash
BACKUP_DIR="/home/user/Pablo/backups"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# Backup SQLite databases
cp /home/user/Pablo/nevesty-models/data.db "$BACKUP_DIR/data_$DATE.db"
[ -f /home/user/Pablo/factory/factory.db ] && cp /home/user/Pablo/factory/factory.db "$BACKUP_DIR/factory_$DATE.db"

# Keep only last 7 days of backups
find "$BACKUP_DIR" -name "*.db" -mtime +7 -delete

echo "Backup completed: $DATE"
