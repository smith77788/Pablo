-- schema_v82: infra_pressure_cache — кешированный Infrastructure Pressure Score per owner
CREATE TABLE IF NOT EXISTS infra_pressure_cache (
    owner_id      BIGINT PRIMARY KEY,
    pressure_score INT NOT NULL DEFAULT 0,
    breakdown     TEXT NOT NULL DEFAULT '{}',
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
