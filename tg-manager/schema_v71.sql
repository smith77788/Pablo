-- schema_v71: Intent Engine — intents table for Epoch IV

CREATE TABLE IF NOT EXISTS intents (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    intent_type   TEXT NOT NULL,              -- presence | network | sync | audit | growth | strike | custom
    description   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',  -- draft | ready | executing | completed | failed | cancelled
    plan          JSONB NOT NULL DEFAULT '{}',
    strategy      TEXT NOT NULL DEFAULT 'balanced', -- safest | balanced | fastest | scalable
    forecast      JSONB NOT NULL DEFAULT '{}',
    feedback      JSONB NOT NULL DEFAULT '{}',     -- actual outcome for memory loop
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    executed_at   TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS intents_owner_status_idx ON intents (owner_id, status, created_at DESC);
