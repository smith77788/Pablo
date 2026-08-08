-- schema_v115: Account Shield — proactive protection config + action log

CREATE TABLE IF NOT EXISTS shield_configs (
    owner_id            BIGINT PRIMARY KEY,
    risk_threshold      FLOAT   DEFAULT 0.7,
    ban_prob_threshold  FLOAT   DEFAULT 0.5,
    auto_pause          BOOLEAN DEFAULT TRUE,
    notify_admin        BOOLEAN DEFAULT TRUE,
    cool_duration_hours INT     DEFAULT 24,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shield_actions (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT       NOT NULL,
    account_id      BIGINT       NOT NULL,
    action          VARCHAR(16)  NOT NULL,  -- ok/warn/cool/pause
    risk_score      FLOAT,
    ban_probability FLOAT,
    note            TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shield_actions_owner_time
    ON shield_actions(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_shield_actions_account
    ON shield_actions(account_id, created_at DESC);
