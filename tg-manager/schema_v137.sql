-- schema_v137: Missing tables for Global Presence, Ecosystems, and Channel Members

-- Channel members tracking (used by topology and audience overlap analysis)
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    joined_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (channel_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_members_user ON channel_members(user_id);
CREATE INDEX IF NOT EXISTS idx_channel_members_channel ON channel_members(channel_id);

-- Ecosystem bots binding
CREATE TABLE IF NOT EXISTS ecosystem_bots (
    ecosystem_id BIGINT NOT NULL,
    bot_id       BIGINT NOT NULL,
    added_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ecosystem_id, bot_id)
);

-- Ecosystem channels binding
CREATE TABLE IF NOT EXISTS ecosystem_channels (
    ecosystem_id BIGINT NOT NULL,
    channel_id   BIGINT NOT NULL,
    added_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ecosystem_id, channel_id)
);

-- Global Presence plans linked to ecosystems
CREATE TABLE IF NOT EXISTS ecosystem_global_presence (
    ecosystem_id BIGINT NOT NULL,
    plan_id      BIGINT NOT NULL,
    added_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ecosystem_id, plan_id)
);
