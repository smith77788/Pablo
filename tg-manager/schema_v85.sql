-- Phase 1-3 infrastructure protection schema
-- trust_level: NEW | WARMED | ELITE (computed from trust_score by trust_engine)

ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS trust_level TEXT NOT NULL DEFAULT 'NEW'
    CHECK (trust_level IN ('NEW', 'WARMED', 'ELITE'));

-- Index for priority scheduling (op_worker picks ELITE first)
CREATE INDEX IF NOT EXISTS idx_tg_accounts_trust_level
    ON tg_accounts (trust_level, trust_score DESC)
    WHERE is_active = TRUE;

-- Table for distributed flood state (Redis fallback / persistence layer)
CREATE TABLE IF NOT EXISTS flood_state_cache (
    account_id      BIGINT PRIMARY KEY,
    cooldown_until  TIMESTAMPTZ,
    consecutive     INT NOT NULL DEFAULT 0,
    risk_score      FLOAT NOT NULL DEFAULT 0.0,
    action_delays   JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_flood_state_updated
    ON flood_state_cache (updated_at);
