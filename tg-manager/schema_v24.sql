-- Trust score per TG account
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS trust_score      FLOAT   NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS flood_count_7d   INT     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cooldown_until   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_flood_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS account_notes    TEXT;

-- Index for fast trusted-account lookup
CREATE INDEX IF NOT EXISTS idx_tg_accounts_trust
    ON tg_accounts(owner_id, is_active, trust_score DESC)
    WHERE is_active = true;

-- Flood events log
CREATE TABLE IF NOT EXISTS account_flood_log (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    operation     TEXT,
    flood_seconds INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
