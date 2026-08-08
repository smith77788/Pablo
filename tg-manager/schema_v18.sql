-- v18: Search Observability & Change Detection System
-- Immutable event → observation → state → change event → alert pipeline

-- Raw immutable snapshots (append-only, never mutated after insert)
CREATE TABLE IF NOT EXISTS search_snapshots (
    snapshot_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL,
    keyword_id    INTEGER NOT NULL REFERENCES tracked_keywords(id) ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    keyword       TEXT NOT NULL,
    results       JSONB NOT NULL,
    result_count  INTEGER NOT NULL,
    truncated     BOOLEAN NOT NULL DEFAULT FALSE,
    search_limit  INTEGER NOT NULL DEFAULT 20,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_account
    ON search_snapshots(keyword_id, account_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_run
    ON search_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_time
    ON search_snapshots(keyword_id, captured_at DESC);

-- Deterministic fact extraction per (snapshot × entity)
CREATE TABLE IF NOT EXISTS search_observations (
    id           BIGSERIAL PRIMARY KEY,
    snapshot_id  UUID NOT NULL REFERENCES search_snapshots(snapshot_id) ON DELETE CASCADE,
    entity_id    TEXT NOT NULL,
    found        BOOLEAN NOT NULL,
    rank         INTEGER,        -- null if not found; first occurrence index + 1
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_snapshot_entity
    ON search_observations(snapshot_id, entity_id);
CREATE INDEX IF NOT EXISTS idx_observations_entity_time
    ON search_observations(entity_id, observed_at DESC);

-- Per (keyword × entity × account) last observed state.
-- NOT authoritative — cache only. Never used as authoritative data source.
CREATE TABLE IF NOT EXISTS observation_state (
    keyword_id      INTEGER NOT NULL REFERENCES tracked_keywords(id) ON DELETE CASCADE,
    entity_id       TEXT NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    last_rank       INTEGER,
    last_found      BOOLEAN NOT NULL DEFAULT FALSE,
    last_snapshot_id UUID,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (keyword_id, entity_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_obs_state_keyword
    ON observation_state(keyword_id);

-- All detected state transitions. Unconfirmed by default.
-- Events are confirmed when a subsequent independent observation agrees.
CREATE TABLE IF NOT EXISTS search_change_events (
    id                    BIGSERIAL PRIMARY KEY,
    run_id                UUID NOT NULL,
    snapshot_id           UUID NOT NULL,
    keyword_id            INTEGER NOT NULL REFERENCES tracked_keywords(id) ON DELETE CASCADE,
    entity_id             TEXT NOT NULL,
    account_id            INTEGER NOT NULL,
    event_type            TEXT NOT NULL
                          CHECK (event_type IN ('APPEARED','DISAPPEARED','POSITION_CHANGED')),
    old_rank              INTEGER,
    new_rank              INTEGER,
    occurred_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Confirmation state
    confirmed             BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_at          TIMESTAMPTZ,
    confirming_snapshot_id UUID,
    -- Alert state
    alerted               BOOLEAN NOT NULL DEFAULT FALSE,
    alerted_at            TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_change_events_keyword
    ON search_change_events(keyword_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_events_pending
    ON search_change_events(confirmed, occurred_at)
    WHERE confirmed = FALSE;

-- Per (keyword × entity × event_type) cooldown to prevent alert spam
CREATE TABLE IF NOT EXISTS search_alert_cooldown (
    keyword_id   INTEGER NOT NULL REFERENCES tracked_keywords(id) ON DELETE CASCADE,
    entity_id    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    last_alerted TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (keyword_id, entity_id, event_type)
);
