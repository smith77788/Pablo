-- schema_v81: proxy_quality_log — per-proxy quality telemetry (used by log_proxy_quality / get_proxy_quality_stats in db.py)
CREATE TABLE IF NOT EXISTS proxy_quality_log (
    id          BIGSERIAL PRIMARY KEY,
    proxy_id    BIGINT NOT NULL,
    latency_ms  INT,
    success     BOOLEAN NOT NULL,
    error_msg   TEXT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_proxy_quality_proxy ON proxy_quality_log(proxy_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_proxy_quality_recent ON proxy_quality_log(checked_at DESC);
