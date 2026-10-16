CREATE TABLE IF NOT EXISTS message_templates (
    id         SERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    name       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(owner_id, name)
);
CREATE INDEX IF NOT EXISTS idx_templates_owner ON message_templates(owner_id);

CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
    id           SERIAL PRIMARY KEY,
    bot_id       BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    message_text TEXT NOT NULL,
    execute_at   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_by   BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scheduled_bot_id ON scheduled_broadcasts(bot_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_status ON scheduled_broadcasts(status);
