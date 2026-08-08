#!/usr/bin/env bash
# Watchdog — запускается отдельно от cron, перезапускает бота при падении.
# Использовать только если НЕТ systemd или Docker.
# Добавить в cron: */2 * * * * /opt/tg-manager/deploy/scripts/watchdog.sh >> /var/log/tg-manager-watchdog.log 2>&1

APP_DIR=/opt/tg-manager
PID_FILE=/var/run/tg-manager.pid
LOCK_FILE=/tmp/tg-manager-watchdog.lock

# Предотвратить одновременный запуск нескольких копий
exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start_bot() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Запуск TG Manager..."
    cd "$APP_DIR"
    source .env 2>/dev/null || true
    nohup "$APP_DIR/venv/bin/python" main.py >> /var/log/tg-manager.log 2>&1 &
    echo $! > "$PID_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Запущен PID=$(cat "$PID_FILE")"
}

if is_running; then
    : # Бот работает, всё хорошо
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Бот не запущен — перезапускаю."
    start_bot
fi
