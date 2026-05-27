-- Мониторинг конкурирующих каналов
CREATE TABLE IF NOT EXISTS competitors (
    id           SERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    username     TEXT NOT NULL,
    label        TEXT,
    channel_id   BIGINT,
    last_members INT,
    last_checked TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, username)
);

CREATE INDEX IF NOT EXISTS idx_competitors_owner ON competitors(owner_id);
