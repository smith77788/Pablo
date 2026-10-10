-- schema_v83: missing base tables — broadcasts, bot_users, post_templates, tg_channels, user_payment_methods
-- All were referenced in db.py/services but CREATE TABLE was absent from all schema files.
-- broadcasts/bot_users had only ALTER TABLEs (v6, v8, v56) with no base CREATE TABLE.

CREATE TABLE IF NOT EXISTS broadcasts (
    id            BIGSERIAL PRIMARY KEY,
    bot_id        BIGINT NOT NULL,
    message_text  TEXT NOT NULL,
    total_users   INT NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_by    BIGINT NOT NULL,
    photo_file_id TEXT,
    sent_count    INT NOT NULL DEFAULT 0,
    failed_count  INT NOT NULL DEFAULT 0,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_broadcasts_bot    ON broadcasts(bot_id);
CREATE INDEX IF NOT EXISTS idx_broadcasts_status ON broadcasts(status);

CREATE TABLE IF NOT EXISTS bot_users (
    bot_id        BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    language_code TEXT,
    phone         TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    is_blocked    BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bot_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_users_active ON bot_users(bot_id, is_active);
CREATE INDEX IF NOT EXISTS idx_bot_users_phone  ON bot_users(phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS post_templates (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    title      TEXT NOT NULL,
    content    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_post_templates_owner ON post_templates(owner_id);

CREATE TABLE IF NOT EXISTS tg_channels (
    id         BIGINT PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    title      TEXT,
    username   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tg_channels_owner ON tg_channels(owner_id);

CREATE TABLE IF NOT EXISTS user_payment_methods (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    method_type TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_pm_owner ON user_payment_methods(owner_id, method_type);
