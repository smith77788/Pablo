-- schema_v47: Presence Packs + Bot Admin Sessions

-- Secret admin tokens for bot owners to access admin panel via /admin TOKEN
CREATE TABLE IF NOT EXISTS bot_admin_sessions (
    bot_id      BIGINT PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bas_token ON bot_admin_sessions(token);

-- Presence Packs: cohesive groups of bot + channels + groups with funnel logic
CREATE TABLE IF NOT EXISTS presence_packs (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    target_url      TEXT,         -- main conversion target URL or @username
    target_label    TEXT,         -- human-readable label for target
    bot_id          BIGINT,       -- linked managed_bot.bot_id
    bot_username    TEXT,         -- cached bot @username
    channel_ids     JSONB DEFAULT '[]',   -- managed_channels.id list
    group_ids       JSONB DEFAULT '[]',   -- managed_channels.id list (megagroups)
    seed_posted     BOOLEAN DEFAULT FALSE,
    bot_promoted    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pp_owner ON presence_packs(owner_id);
