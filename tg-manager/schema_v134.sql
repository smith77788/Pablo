-- schema_v134: Adaptive Pacing, Channel Privacy, Strike History, Proxy Stats

-- Mark private channels to avoid repeated access attempts
ALTER TABLE tg_channels ADD COLUMN IF NOT EXISTS is_private BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_tg_channels_private ON tg_channels(owner_id, is_private) WHERE is_private = TRUE;

-- Strike history: prevent striking the same target too frequently
CREATE TABLE IF NOT EXISTS strike_history (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    target      TEXT NOT NULL,
    target_type TEXT DEFAULT 'channel',
    result      TEXT,
    accounts_used INT[],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strike_history_target ON strike_history(target, created_at);
CREATE INDEX IF NOT EXISTS idx_strike_history_owner ON strike_history(owner_id, created_at);

-- Proxy performance tracking
CREATE TABLE IF NOT EXISTS proxy_performance (
    id          BIGSERIAL PRIMARY KEY,
    proxy_url   TEXT NOT NULL,
    test_type   TEXT NOT NULL DEFAULT 'connectivity',
    latency_ms  INT,
    success     BOOLEAN NOT NULL DEFAULT TRUE,
    error_msg   TEXT,
    tested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_proxy_perf_url ON proxy_performance(proxy_url, tested_at);

-- Adaptive pacing logs: track what delays worked and what triggered bans
CREATE TABLE IF NOT EXISTS adaptive_pacing_log (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    operation_id    BIGINT,
    base_delay      FLOAT,
    actual_delay    FLOAT,
    hour_of_day     INT,
    day_of_week     INT,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pacing_log_owner ON adaptive_pacing_log(owner_id, created_at);

-- Account usage tracking for intelligent rotation
CREATE TABLE IF NOT EXISTS account_usage_log (
    id              BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL,
    operation_type  TEXT NOT NULL,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    duration_ms     INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_usage_log_account ON account_usage_log(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_log_type ON account_usage_log(operation_type, created_at);
