-- Run once before first launch

CREATE TABLE IF NOT EXISTS managed_bots (
    id          SERIAL PRIMARY KEY,
    token       TEXT        UNIQUE NOT NULL,
    bot_id      BIGINT      UNIQUE NOT NULL,
    username    TEXT,
    first_name  TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    added_by    BIGINT      NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_users (
    id            SERIAL PRIMARY KEY,
    bot_id        BIGINT      NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    user_id       BIGINT      NOT NULL,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    language_code TEXT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    UNIQUE (bot_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_users_bot_id ON bot_users(bot_id);
CREATE INDEX IF NOT EXISTS idx_bot_users_user_id ON bot_users(user_id);

CREATE TABLE IF NOT EXISTS broadcasts (
    id          SERIAL PRIMARY KEY,
    bot_id      BIGINT      NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    message_text TEXT       NOT NULL,
    total_users INT         NOT NULL DEFAULT 0,
    sent_count  INT         NOT NULL DEFAULT 0,
    failed_count INT        NOT NULL DEFAULT 0,
    status      TEXT        NOT NULL DEFAULT 'pending',  -- pending|running|done|cancelled
    created_by  BIGINT      NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
