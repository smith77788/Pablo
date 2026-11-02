-- schema_v291: тихие часы уведомлений и сводка за них (services/quiet_hours.py).
--
-- Уведомления раньше шли круглосуточно: итог ночной операции или новый
-- подписчик будили владельца в три часа ночи, а выключить можно было только
-- тип целиком. Лидеры дают «не беспокоить» с утренней сводкой. Здесь — окно
-- тишины в часовом поясе владельца и очередь придержанного: в тихие часы
-- несрочное складывается сюда и приходит одним сообщением, когда окно
-- закончится. Срочное (ограничения и флуд аккаунтов) идёт сразу.
ALTER TABLE notification_settings
    ADD COLUMN IF NOT EXISTS quiet_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS quiet_start   SMALLINT NOT NULL DEFAULT 23
        CHECK (quiet_start BETWEEN 0 AND 23),
    ADD COLUMN IF NOT EXISTS quiet_end     SMALLINT NOT NULL DEFAULT 8
        CHECK (quiet_end BETWEEN 0 AND 23),
    ADD COLUMN IF NOT EXISTS tz            TEXT NOT NULL DEFAULT 'Europe/Moscow';

CREATE TABLE IF NOT EXISTS notification_held (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    pref       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notification_held_user
    ON notification_held(user_id, id);
