-- schema_v113: Physics Engine — operational telemetry + account risk scoring

CREATE TABLE IF NOT EXISTS op_telemetry (
    id           BIGSERIAL PRIMARY KEY,
    account_id   BIGINT NOT NULL,
    owner_id     BIGINT,
    op_type      TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('success','flood_wait','ban','error')),
    flood_wait_s INT  NOT NULL DEFAULT 0,
    duration_ms  INT  NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS op_telemetry_account_idx ON op_telemetry(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS op_telemetry_type_idx    ON op_telemetry(op_type, created_at DESC);
CREATE INDEX IF NOT EXISTS op_telemetry_created_idx ON op_telemetry(created_at DESC);

CREATE TABLE IF NOT EXISTS account_risk_scores (
    account_id      BIGINT PRIMARY KEY,
    risk_score      FLOAT NOT NULL DEFAULT 0.0,
    ban_probability FLOAT NOT NULL DEFAULT 0.0,
    flood_rate_1h   FLOAT NOT NULL DEFAULT 0.0,
    ops_24h         INT   NOT NULL DEFAULT 0,
    last_flood_at   TIMESTAMPTZ,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
