-- schema_v107.sql: Clone & Adapt — bot profile cloning history

CREATE TABLE IF NOT EXISTS clone_adapt_history (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    source_bot_id   BIGINT NOT NULL,
    target_bot_id   BIGINT NOT NULL,
    fields          TEXT NOT NULL,   -- comma-separated: name,desc,short,photo,commands
    status          TEXT NOT NULL,   -- ok|error
    details         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS clone_adapt_history_owner_idx ON clone_adapt_history(owner_id);
CREATE INDEX IF NOT EXISTS clone_adapt_history_created_idx ON clone_adapt_history(created_at DESC);
