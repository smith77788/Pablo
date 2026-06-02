-- schema_v60: Account Infrastructure — Tags/Pools, CRM Labels, Proxy Quality, Pressure Cache

-- System 1: Account Tags & Pools
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS pool TEXT DEFAULT NULL;

-- System 3: Account CRM
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS labels TEXT[] DEFAULT '{}';
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS warnings TEXT[] DEFAULT '{}';
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS project TEXT DEFAULT NULL;

-- System 4: Proxy Intelligence — latency / error history
CREATE TABLE IF NOT EXISTS proxy_quality_log (
    id          SERIAL PRIMARY KEY,
    proxy_id    INT REFERENCES user_proxies(id) ON DELETE CASCADE,
    latency_ms  INT,
    success     BOOL NOT NULL,
    error_msg   TEXT,
    checked_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pql_proxy_checked ON proxy_quality_log(proxy_id, checked_at DESC);

-- System 2: Infrastructure Pressure Score cache (per owner)
CREATE TABLE IF NOT EXISTS infra_pressure_cache (
    owner_id     BIGINT PRIMARY KEY,
    pressure_score INT NOT NULL DEFAULT 0,
    breakdown    JSONB,
    computed_at  TIMESTAMPTZ DEFAULT NOW()
);
