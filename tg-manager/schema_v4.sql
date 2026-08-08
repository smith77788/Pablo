CREATE TABLE IF NOT EXISTS relay_sessions (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    username TEXT,
    first_name TEXT,
    last_activity TIMESTAMPTZ NOT NULL DEFAULT now(),
    messages_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS relay_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES relay_sessions(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    text TEXT NOT NULL,
    forwarded_msg_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS relay_enabled BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_relay_messages_fwd ON relay_messages(forwarded_msg_id) WHERE forwarded_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relay_sessions_bot ON relay_sessions(bot_id, last_activity DESC);
