-- schema_v95: Infrastructure-as-Radar — track all entities seen across all client sessions
-- Every time our Telethon clients encounter a user/channel/group, we log it once per day.
-- This builds a "radar" of seen entities enabling:
--   • "Замечен в X чатах" count
--   • First/last seen timestamps across our infrastructure
--   • Cross-reference: which of our sessions have seen this user

CREATE TABLE IF NOT EXISTS seen_entities (
    entity_id       BIGINT       NOT NULL,
    entity_type     TEXT         NOT NULL CHECK (entity_type IN ('user', 'bot', 'channel', 'group')),
    chat_id         BIGINT,                           -- which chat/dialog we saw them in (NULL = direct lookup)
    seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sighting_count  INTEGER      NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_id, chat_id)
);

-- Efficient lookup: how many distinct chats has this entity been seen in?
CREATE INDEX IF NOT EXISTS idx_seen_entities_entity ON seen_entities (entity_id);
CREATE INDEX IF NOT EXISTS idx_seen_entities_chat   ON seen_entities (chat_id);
CREATE INDEX IF NOT EXISTS idx_seen_entities_last   ON seen_entities (last_seen_at);

-- Materialized per-entity aggregate: total distinct chats seen in
CREATE TABLE IF NOT EXISTS entity_radar_stats (
    entity_id       BIGINT       PRIMARY KEY,
    entity_type     TEXT         NOT NULL,
    first_seen_at   TIMESTAMPTZ  NOT NULL,
    last_seen_at    TIMESTAMPTZ  NOT NULL,
    distinct_chats  INTEGER      NOT NULL DEFAULT 0,
    total_sightings INTEGER      NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_radar_first ON entity_radar_stats (first_seen_at);
CREATE INDEX IF NOT EXISTS idx_entity_radar_chats ON entity_radar_stats (distinct_chats DESC);
