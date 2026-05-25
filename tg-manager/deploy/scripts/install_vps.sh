#!/usr/bin/env bash
# Полная установка TG Manager на чистый Ubuntu 22.04 / Debian 12 VPS
# Запускать от root: bash install_vps.sh
set -euo pipefail

APP_DIR=/opt/tg-manager
APP_USER=tgmanager

echo "==> Обновление системы..."
apt-get update -qq && apt-get upgrade -y -qq

echo "==> Установка зависимостей..."
apt-get install -y -qq python3.12 python3.12-venv python3.12-dev \
    postgresql postgresql-contrib libpq-dev gcc git curl

echo "==> Запуск PostgreSQL..."
systemctl enable postgresql
systemctl start postgresql

echo "==> Создание пользователя и БД PostgreSQL..."
PG_PASS=$(openssl rand -hex 16)
sudo -u postgres psql -c "CREATE USER $APP_USER WITH PASSWORD '$PG_PASS';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE $APP_USER OWNER $APP_USER;" 2>/dev/null || true
echo "   PostgreSQL пароль: $PG_PASS"

echo "==> Создание системного пользователя..."
useradd --system --shell /bin/bash --home-dir $APP_DIR --create-home $APP_USER 2>/dev/null || true

echo "==> Копирование файлов приложения..."
cp -r . $APP_DIR/
chown -R $APP_USER:$APP_USER $APP_DIR

echo "==> Создание Python virtualenv..."
sudo -u $APP_USER python3.12 -m venv $APP_DIR/venv
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -q --upgrade pip
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -q -r $APP_DIR/requirements.txt

echo "==> Инициализация схемы БД..."
DATABASE_URL="postgresql://$APP_USER:$PG_PASS@localhost:5432/$APP_USER"
for schema in $APP_DIR/schema.sql $APP_DIR/schema_v*.sql; do
    [ -f "$schema" ] && PGPASSWORD="$PG_PASS" psql -U $APP_USER -d $APP_USER -f "$schema" -q 2>/dev/null || true
done

echo "==> Создание .env файла..."
if [ ! -f "$APP_DIR/.env" ]; then
    cat > $APP_DIR/.env <<EOF
MANAGER_BOT_TOKEN=ВСТАВЬ_ТОКЕН_СЮДА
DATABASE_URL=postgresql://$APP_USER:$PG_PASS@localhost:5432/$APP_USER
ADMIN_IDS=
BROADCAST_DELAY=0.05
MAX_CONCURRENT=20
EOF
    chown $APP_USER:$APP_USER $APP_DIR/.env
    chmod 600 $APP_DIR/.env
    echo "   !! Отредактируй $APP_DIR/.env — вставь MANAGER_BOT_TOKEN"
fi

echo "==> Установка systemd сервиса..."
cp $APP_DIR/deploy/systemd/tg-manager.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable tg-manager

echo ""
echo "======================================================"
echo "  Установка завершена!"
echo "  1. Отредактируй: nano $APP_DIR/.env"
echo "  2. Запусти бота: systemctl start tg-manager"
echo "  3. Статус:       systemctl status tg-manager"
echo "  4. Логи:         journalctl -u tg-manager -f"
echo "======================================================"
