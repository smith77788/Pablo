-- schema_v163: «Хранилище» (Echo Vault) — архив сообщений через Telegram Business.
--
-- Пользователь подключает бота в настройках бизнес-аккаунта (Настройки → Telegram
-- для бизнеса → Чат-боты). Бот получает business_message/edited/deleted апдейты и
-- ЗЕРКАЛИТ переписку в архив, который переживает удаление чата у пользователя.
-- Текст/подпись хранятся в ЗАШИФРОВАННОМ виде (token_vault, AES-256-GCM); медиа —
-- ссылкой (file_id) + метаданные (решение по стоимости: без скачивания байтов).

-- Подключения бизнес-аккаунтов к боту (одна строка на business_connection).
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id   TEXT PRIMARY KEY,               -- BusinessConnection.id
    owner_id        BIGINT NOT NULL,                -- Telegram user_id владельца бизнес-аккаунта
    user_chat_id    BIGINT,                         -- личный чат владельца с ботом (для уведомлений)
    can_reply       BOOLEAN NOT NULL DEFAULT FALSE, -- право писать от имени пользователя
    is_enabled      BOOLEAN NOT NULL DEFAULT TRUE,  -- подключение активно (можно временно выключить)
    rights          JSONB,                          -- сырые BusinessBotRights (на будущее)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bizconn_owner ON business_connections(owner_id);

-- Архив сообщений. UNIQUE(owner_id, chat_id, msg_id) → идемпотентность апдейтов и
-- адресация правок/удалений по (чат, message_id).
CREATE TABLE IF NOT EXISTS vault_messages (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    connection_id   TEXT,
    chat_id         BIGINT NOT NULL,                -- собеседник (в ЛС chat.id = его user_id)
    msg_id          BIGINT NOT NULL,                -- message_id внутри бизнес-чата
    direction       TEXT NOT NULL DEFAULT 'in',     -- 'in' (получено) / 'out' (отправлено владельцем)
    peer_user_id    BIGINT,
    peer_name       TEXT,
    peer_username   TEXT,
    text_enc        TEXT,                           -- ENC:<...> текст/подпись (token_vault)
    media_type      TEXT,                           -- photo/video/document/voice/audio/... | NULL
    media_file_id   TEXT,
    media_unique_id TEXT,
    media_size      BIGINT,
    media_mime      TEXT,
    media_name      TEXT,
    msg_date        TIMESTAMPTZ,
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE, -- удалено у пользователя, но осталось в архиве
    deleted_at      TIMESTAMPTZ,
    is_edited       BOOLEAN NOT NULL DEFAULT FALSE,
    edited_at       TIMESTAMPTZ,
    edit_history    JSONB,                          -- прошлые зашифрованные версии текста
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_vault_owner_chat ON vault_messages(owner_id, chat_id, msg_date);
CREATE INDEX IF NOT EXISTS idx_vault_owner_date ON vault_messages(owner_id, msg_date DESC);
