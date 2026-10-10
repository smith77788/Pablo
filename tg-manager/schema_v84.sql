-- schema_v84: activity_log — лог всех действий пользователей в боте
-- Источник: UserActivityLogMiddleware (команды + кнопки + ошибки)
CREATE TABLE IF NOT EXISTS activity_log (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    event_type  TEXT NOT NULL,              -- 'command', 'callback', 'message', 'error'
    action      TEXT NOT NULL,              -- '/start', 'chan:menu', fsm_state, etc.
    detail      TEXT,                       -- безопасный дополнительный контекст
    status      TEXT NOT NULL DEFAULT 'ok', -- 'ok', 'error'
    error_msg   TEXT,
    duration_ms INT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_activity_log_owner  ON activity_log(owner_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_recent ON activity_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_errors ON activity_log(status, occurred_at DESC) WHERE status = 'error';

CREATE TABLE IF NOT EXISTS recovery_events (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    recovery_type  TEXT NOT NULL,
    target_type    TEXT NOT NULL,
    target_id      BIGINT,
    trigger        TEXT NOT NULL DEFAULT 'auto',
    action         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    severity       TEXT NOT NULL DEFAULT 'info',
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome        JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_recovery_events_owner_created
    ON recovery_events(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recovery_events_owner_status
    ON recovery_events(owner_id, status);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id                  BIGSERIAL PRIMARY KEY,
    owner_id            BIGINT NOT NULL,
    anomaly_type        TEXT NOT NULL,
    detector            TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'warning',
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    baseline_value      DOUBLE PRECISION,
    anomaly_value       DOUBLE PRECISION,
    deviation_pct       DOUBLE PRECISION,
    affected_count      INT NOT NULL DEFAULT 0,
    target_ids          JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    triggered_recovery  BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_owner_active
    ON anomaly_events(owner_id, is_active, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_type
    ON anomaly_events(owner_id, anomaly_type, detected_at DESC);

CREATE TABLE IF NOT EXISTS system_health_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    owner_id              BIGINT NOT NULL,
    health_score          INT NOT NULL DEFAULT 0,
    accounts_ready        INT NOT NULL DEFAULT 0,
    accounts_total        INT NOT NULL DEFAULT 0,
    accounts_in_cooldown  INT NOT NULL DEFAULT 0,
    avg_trust_score       DOUBLE PRECISION NOT NULL DEFAULT 0,
    ops_pending           INT NOT NULL DEFAULT 0,
    ops_running           INT NOT NULL DEFAULT 0,
    ops_failed_24h        INT NOT NULL DEFAULT 0,
    ops_done_24h          INT NOT NULL DEFAULT 0,
    proxies_healthy       INT NOT NULL DEFAULT 0,
    proxies_total         INT NOT NULL DEFAULT 0,
    active_alerts         INT NOT NULL DEFAULT 0,
    active_anomalies      INT NOT NULL DEFAULT 0,
    active_recoveries     INT NOT NULL DEFAULT 0,
    components            JSONB NOT NULL DEFAULT '{}'::jsonb,
    snapshot_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_system_health_snapshots_owner_time
    ON system_health_snapshots(owner_id, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS infrastructure_alerts (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    alert_type     TEXT NOT NULL DEFAULT 'general',
    severity       TEXT NOT NULL DEFAULT 'warning',
    title          TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    target_type    TEXT,
    target_id      BIGINT,
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_infrastructure_alerts_owner_active
    ON infrastructure_alerts(owner_id, is_active, first_seen_at DESC);
