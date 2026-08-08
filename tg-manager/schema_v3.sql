CREATE TABLE IF NOT EXISTS auto_replies (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('start', 'keyword', 'any')),
    keyword TEXT,
    response_text TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_update_offsets (
    bot_id BIGINT PRIMARY KEY REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    last_update_id BIGINT NOT NULL DEFAULT 0
);
