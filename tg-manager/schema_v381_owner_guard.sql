-- schema_v381: код Infragram владельца (services/owner_guard.py), волна 3.
--
-- Вход в мини-апп подтверждает только Telegram. Код — вторая стена перед
-- сливом данных (экспорт сессии, показ паролей 2FA, удаление): по желанию,
-- но когда поставлен — спрашивается перед опасным действием.
--
-- owner_guard — сам код (scrypt-хеш) и счётчик промахов с замком.
-- owner_sessions.pin_ok_until — подтверждение кода У КОНКРЕТНОГО сеанса
--   (services/owner_sessions): чужой сеанс угонщика им не воспользуется.
CREATE TABLE IF NOT EXISTS owner_guard (
    owner_id        BIGINT      PRIMARY KEY,
    pin_hash        TEXT,
    failed_attempts INT         NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE owner_sessions ADD COLUMN IF NOT EXISTS pin_ok_until TIMESTAMPTZ;
