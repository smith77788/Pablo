-- v199: общие папки (Shared Folders) как канал инвайтинга.
--
-- Владелец собирает свои каналы/чат в общую папку Telegram и раздаёт ОДНУ
-- chatlist-ссылку — по ней человек добавляет сразу всю связку. Здесь хранится
-- сама папка (набор чатов + экспортированная ссылка) и счётчик добавлений.
--
-- Ссылку экспортирует сессия аккаунта-владельца (это Operation), поэтому у
-- папки есть аккаунт-владелец сессии и статус готовности ссылки.
CREATE TABLE IF NOT EXISTS chatlist_folders (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    acc_id        BIGINT,                    -- аккаунт, чьей сессией создана папка
    title         TEXT NOT NULL,
    chat_ids      BIGINT[] NOT NULL DEFAULT '{}',
    chat_count    INTEGER NOT NULL DEFAULT 0,
    -- filter_id папки в Telegram-клиенте аккаунта (2..255) — нужен для повторного
    -- экспорта/отзыва ссылки той же папки, а не создания новой при каждом клике.
    filter_id     INTEGER,
    invite_link   TEXT,
    invite_slug   TEXT,                      -- хвост addlist-ссылки, для дедупа
    status        TEXT NOT NULL DEFAULT 'draft',  -- draft|ready|failed
    error         TEXT,
    -- Привязка к связке (network_builder): «развернул связку → раздал папку».
    instance_id   BIGINT,
    join_count    INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatlist_folders_owner
    ON chatlist_folders(owner_id, created_at DESC);
