#!/usr/bin/env bash
# Точка входа контейнера: подготовить каталоги на запись и сбросить root.
#
# ЗАЧЕМ. Раньше весь контейнер (бот, мини-апп, ассистент) работал от root.
# Уязвимость в любом из ~300 сервисов или в зависимости давала root внутри
# контейнера: запись в код приложения, в /etc, установка пакетов.
#
# ПОЧЕМУ НЕ ПРОСТО "USER app" В Dockerfile. Тома Railway монтируются от root.
# Процесс, запущенный сразу от app, не смог бы писать в смонтированный том
# (состояние ассистента и т. п.). Поэтому стартуем root'ом только на время
# chown нужных каталогов, затем exec через setpriv от пользователя app —
# дальше root в процессе не остаётся.
#
# Аварийный выход: INFRAGRAM_RUN_AS_ROOT=1 в переменных Railway возвращает
# прежнее поведение без пересборки образа.
set -u

APP_USER="${APP_USER:-app}"

if [ "$(id -u)" != "0" ] || [ "${INFRAGRAM_RUN_AS_ROOT:-0}" = "1" ]; then
  exec bash /app/start-all.sh
fi

# Каталоги, куда приложение пишет. /tmp у app и так есть.
for d in /app/pablo/data "${ASSISTANT_STATE_DIR:-}" "${RAILWAY_VOLUME_MOUNT_PATH:-}"; do
  [ -n "$d" ] || continue
  mkdir -p "$d" 2>/dev/null || true
  chown -R "$APP_USER:$APP_USER" "$d" 2>/dev/null \
    || echo "[entrypoint] не удалось отдать $d пользователю $APP_USER"
done

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "[entrypoint] пользователя $APP_USER нет, запуск от root"
  exec bash /app/start-all.sh
fi

exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups \
  --inh-caps=-all --bounding-set=-all --no-new-privs \
  env HOME="/home/$APP_USER" bash /app/start-all.sh
