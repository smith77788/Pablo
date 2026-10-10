-- Telegram-облако: файлы шифруются (AES-256-GCM), режутся на куски и
-- распределяются по хранилищу (v1 — БД-бэкенд; далее — приватные каналы флота).
-- Собираются обратно по запросу. Доступ: владелец платформы — безлимит (личное
-- пользование), остальным — платно (tg_cloud_quota.paid / тариф).
-- Файлы больших размеров хранит распределённый транспорт; в БД — только манифест.

CREATE TABLE IF NOT EXISTS tg_cloud_files (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    size_bytes  BIGINT NOT NULL DEFAULT 0,
    chunk_count INT    NOT NULL DEFAULT 0,
    sha256      TEXT   NOT NULL DEFAULT '',        -- целостность всего файла
    status      TEXT   NOT NULL DEFAULT 'pending', -- pending|stored|failed|deleting
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tg_cloud_files_owner ON tg_cloud_files(owner_id, id DESC);

-- Кусок файла: где лежит (аккаунт/канал/сообщение) + порядок/размер/хэш.
CREATE TABLE IF NOT EXISTS tg_cloud_chunks (
    id          BIGSERIAL PRIMARY KEY,
    file_id     BIGINT NOT NULL REFERENCES tg_cloud_files(id) ON DELETE CASCADE,
    owner_id    BIGINT NOT NULL,
    ord         INT    NOT NULL,
    size_bytes  BIGINT NOT NULL DEFAULT 0,
    sha256      TEXT   NOT NULL DEFAULT '',
    acc_id      BIGINT,          -- аккаунт-хранитель (tg_accounts.id), для флот-транспорта
    channel_id  BIGINT,          -- приватный канал
    message_id  BIGINT,          -- сообщение с медиа
    locator     TEXT   NOT NULL DEFAULT '', -- ключ хранения (транспорт-специфично)
    status      TEXT   NOT NULL DEFAULT 'pending',
    UNIQUE(file_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_tg_cloud_chunks_file ON tg_cloud_chunks(file_id, ord);

-- Квота/оплата доступа к облаку по владельцу.
CREATE TABLE IF NOT EXISTS tg_cloud_quota (
    owner_id    BIGINT PRIMARY KEY,
    limit_bytes BIGINT  NOT NULL DEFAULT 0,   -- 0 = без явного лимита (для платных/админа)
    used_bytes  BIGINT  NOT NULL DEFAULT 0,
    paid        BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- v1-бэкенд хранения кусков (БД). Флот-транспорт (каналы аккаунтов) заменит его,
-- не меняя манифест/API. Хранит зашифрованный blob куска.
CREATE TABLE IF NOT EXISTS tg_cloud_blobs (
    locator     TEXT PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    data        BYTEA  NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
