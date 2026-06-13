-- schema_v96: Username and display name history tracking
-- Records every observed change in username or display name.
-- Enables "username history" and "name history" features like Funstat, but richer.

CREATE TABLE IF NOT EXISTS entity_name_history (
    id            BIGSERIAL     PRIMARY KEY,
    entity_id     BIGINT        NOT NULL,
    entity_type   TEXT          NOT NULL CHECK (entity_type IN ('user', 'bot', 'channel', 'group')),
    username      TEXT,                             -- @handle without @, NULL if none
    display_name  TEXT,                             -- first+last or channel title
    seen_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_name_history_entity ON entity_name_history (entity_id, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_name_history_username ON entity_name_history (username) WHERE username IS NOT NULL;

-- Quick lookup: last known state per entity (avoids full history scan)
CREATE TABLE IF NOT EXISTS entity_last_known (
    entity_id     BIGINT        PRIMARY KEY,
    entity_type   TEXT          NOT NULL,
    username      TEXT,
    display_name  TEXT,
    first_seen_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
