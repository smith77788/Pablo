-- Облако, флот-транспорт: у каждого аккаунта-хранителя — свой приватный канал-
-- «склад», куда он кладёт зашифрованные куски документами. Привязку аккаунт→канал
-- кэшируем, чтобы не создавать канал на каждый кусок. Куски распределяются по
-- РАЗНЫМ аккаунтам (реплики на разных хранителях) — бан одного не рушит файл.
CREATE TABLE IF NOT EXISTS tg_cloud_storekeepers (
    acc_id      BIGINT PRIMARY KEY,              -- tg_accounts.id
    owner_id    BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,                 -- приватный канал-склад этого аккаунта
    access_hash BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tg_cloud_storekeepers_owner ON tg_cloud_storekeepers(owner_id);
