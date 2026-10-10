-- schema_v97: Entity follow / watch list for change notifications
-- Users can subscribe to watch a Telegram entity and receive notifications
-- when username, display name, or other tracked fields change.

CREATE TABLE IF NOT EXISTS entity_follows (
    id          BIGSERIAL     PRIMARY KEY,
    owner_id    BIGINT        NOT NULL,   -- Telegram user_id of the subscriber
    entity_id   BIGINT        NOT NULL,
    entity_type TEXT          NOT NULL CHECK (entity_type IN ('user', 'bot', 'channel', 'group', 'supergroup')),
    label       TEXT,                     -- optional human label ("competitor", "client", etc.)
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    UNIQUE (owner_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_follows_owner   ON entity_follows (owner_id);
CREATE INDEX IF NOT EXISTS idx_entity_follows_entity  ON entity_follows (entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_follows_check   ON entity_follows (last_checked_at NULLS FIRST);

-- Change notifications log
CREATE TABLE IF NOT EXISTS entity_follow_events (
    id          BIGSERIAL     PRIMARY KEY,
    follow_id   BIGINT        NOT NULL REFERENCES entity_follows(id) ON DELETE CASCADE,
    owner_id    BIGINT        NOT NULL,
    entity_id   BIGINT        NOT NULL,
    change_type TEXT          NOT NULL,  -- 'username_changed', 'name_changed', 'both_changed'
    old_username TEXT,
    new_username TEXT,
    old_name    TEXT,
    new_name    TEXT,
    detected_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    notified    BOOLEAN       NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_follow_events_owner     ON entity_follow_events (owner_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_follow_events_unnotified ON entity_follow_events (notified) WHERE NOT notified;
