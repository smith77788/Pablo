-- Account trust score history for trend analysis
CREATE TABLE IF NOT EXISTS account_trust_history (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    owner_id    BIGINT NOT NULL,
    trust_score FLOAT  NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trust_history_account
    ON account_trust_history(account_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_trust_history_owner
    ON account_trust_history(owner_id, recorded_at DESC);

-- Retain only last 30 days per account (TTL via periodic cleanup in trust_engine)
