-- Behavioral Intelligence Layer
-- Raw behavioral event log
CREATE TABLE IF NOT EXISTS behavioral_events (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    entity_type  TEXT NOT NULL,  -- 'bot'|'channel'|'group'|'keyword'
    entity_id    BIGINT NOT NULL,
    event_type   TEXT NOT NULL,  -- 'reentry'|'search_repeat'|'cross_nav'|'habit_signal'
    session_id   TEXT,
    meta         JSONB DEFAULT '{}',
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_be_owner_entity
    ON behavioral_events(owner_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_be_occurred
    ON behavioral_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_be_owner_type
    ON behavioral_events(owner_id, event_type, occurred_at DESC);

-- Computed behavioral scores per entity
CREATE TABLE IF NOT EXISTS entity_behavioral_score (
    owner_id         BIGINT NOT NULL,
    entity_type      TEXT NOT NULL,
    entity_id        BIGINT NOT NULL,
    attention_score  NUMERIC(5,2) NOT NULL DEFAULT 0,
    habit_score      NUMERIC(5,2) NOT NULL DEFAULT 0,
    ecosystem_score  NUMERIC(5,2) NOT NULL DEFAULT 0,
    decay_rate       NUMERIC(5,4) NOT NULL DEFAULT 0,
    last_reentry_at  TIMESTAMPTZ,
    reentry_count    INT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, entity_type, entity_id)
);

-- Persistent search memory / keyword affinity
CREATE TABLE IF NOT EXISTS search_memory (
    owner_id       BIGINT NOT NULL,
    keyword        TEXT NOT NULL,
    search_count   INT NOT NULL DEFAULT 1,
    last_searched  TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_searched TIMESTAMPTZ NOT NULL DEFAULT now(),
    affinity_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_search_memory_owner
    ON search_memory(owner_id, affinity_score DESC);
