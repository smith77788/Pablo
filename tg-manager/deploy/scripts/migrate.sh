#!/usr/bin/env bash
# Применить все новые миграции к существующей БД.
# Использование: bash deploy/scripts/migrate.sh
# Переменные берутся из .env или передаются через окружение.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Загрузить .env если есть
if [ -f "$APP_DIR/.env" ]; then
    set -a; source "$APP_DIR/.env"; set +a
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL не задан. Задай в .env или окружении."
    exit 1
fi

echo "==> Применяем миграции к: $DATABASE_URL"

for schema in "$APP_DIR"/schema.sql "$APP_DIR"/schema_v*.sql; do
    [ -f "$schema" ] || continue
    name=$(basename "$schema")
    echo "  -> $name"
    psql "$DATABASE_URL" -f "$schema" -q 2>&1 | grep -v "^$" | grep -v "already exists" || true
done

echo "==> Готово."
