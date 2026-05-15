#!/bin/bash
# VACUUM SQLite database to reclaim space and rebuild indexes
# Run weekly or manually after large deletes
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
DB_PATH="${DB_PATH:-$APP_DIR/data.db}"

if [ ! -f "$DB_PATH" ]; then
  echo "Database not found: $DB_PATH"
  exit 1
fi

if command -v sqlite3 &>/dev/null; then
  echo "Running WAL checkpoint + VACUUM on $DB_PATH ..."
  sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);"
  sqlite3 "$DB_PATH" "VACUUM;"
  echo "VACUUM completed successfully"
else
  echo "sqlite3 not found — skipping VACUUM (will be done by Node.js scheduler)"
fi
