-- EPOCH VI: Self-Healing Infrastructure Schema (v78)
-- recovery_events, infrastructure_alerts, anomaly_events, system_health_snapshots

-- ─────────────────────────────────────────────────────────────────────────
-- Recovery events log — каждое действие по восстановлению инфраструктуры
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS recovery_events (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(id) ON DELETE CASCADE,
    recovery_type   TEXT NOT NULL,  -- account | proxy | session | queue | worker | operation
    target_type     TEXT NOT NULL,  -- account | proxy | operation | system
    target_id       BIGINT,         -- ID объекта восстановления (account_id, proxy_id, op_id)
    trigger         TEXT NOT NULL,  -- auto | manual | anomaly | copilot
    action          TEXT NOT NULL,  -- exclude | reassign | reconnect | restart | escalate | resume
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | success | failed | skipped
    severity        TEXT NOT NULL DEFAULT 'warning',  -- info | warning | critical
    details         JSONB DEFAULT '{}',  -- контекст: что именно произошло
    outcome         JSONB DEFAULT '{}',  -- результат: что было сделано, сколько восстановлено
    error_msg       TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_recovery_events_owner ON recovery_events(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recovery_events_type ON recovery_events(recovery_type, status);
CREATE INDEX IF NOT EXISTS idx_recovery_events_target ON recovery_events(target_type, target_id);

-- ─────────────────────────────────────────────────────────────────────────
-- Infrastructure alerts — активные проблемы с инфраструктурой
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS infrastructure_alerts (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(id) ON DELETE CASCADE,
    alert_type      TEXT NOT NULL,  -- account_degradation | proxy_failure | queue_overflow | anomaly | capacity
    severity        TEXT NOT NULL,  -- info | warning | critical
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    recommendation  TEXT,
    target_type     TEXT,           -- account | proxy | queue | system
    target_id       BIGINT,
    is_active       BOOLEAN DEFAULT TRUE,
    auto_recovering BOOLEAN DEFAULT FALSE,  -- идёт ли авто-восстановление
    recovery_event_id BIGINT REFERENCES recovery_events(id) ON DELETE SET NULL,
    first_seen_at   TIMESTAMPTZ DEFAULT now(),
    last_seen_at    TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,           -- auto | manual | timeout
    snoozed_until   TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_infra_alerts_owner ON infrastructure_alerts(owner_id, is_active);
CREATE INDEX IF NOT EXISTS idx_infra_alerts_type ON infrastructure_alerts(alert_type, severity);
CREATE INDEX IF NOT EXISTS idx_infra_alerts_active ON infrastructure_alerts(owner_id, is_active, severity);

-- ─────────────────────────────────────────────────────────────────────────
-- Anomaly events — обнаруженные аномалии паттернов
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS anomaly_events (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(id) ON DELETE CASCADE,
    anomaly_type    TEXT NOT NULL,  -- error_spike | success_drop | queue_surge | latency_spike | flood_wave | trust_collapse
    detector        TEXT NOT NULL,  -- account | proxy | queue | timing
    severity        TEXT NOT NULL DEFAULT 'warning',
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    baseline_value  FLOAT,          -- нормальное значение
    anomaly_value   FLOAT,          -- аномальное значение
    deviation_pct   FLOAT,          -- процент отклонения от нормы
    affected_count  INTEGER DEFAULT 0,   -- сколько объектов затронуто
    target_ids      JSONB DEFAULT '[]',  -- [account_id, ...] или [proxy_id, ...]
    is_active       BOOLEAN DEFAULT TRUE,
    triggered_recovery BOOLEAN DEFAULT FALSE,
    recovery_event_id  BIGINT REFERENCES recovery_events(id) ON DELETE SET NULL,
    detected_at     TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_anomaly_events_owner ON anomaly_events(owner_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_active ON anomaly_events(owner_id, is_active, severity);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_type ON anomaly_events(anomaly_type, detector);

-- ─────────────────────────────────────────────────────────────────────────
-- System health snapshots — почасовые снапшоты состояния системы
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_health_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(id) ON DELETE CASCADE,
    health_score    INTEGER NOT NULL,       -- 0-100
    accounts_ready  INTEGER DEFAULT 0,
    accounts_total  INTEGER DEFAULT 0,
    accounts_in_cooldown INTEGER DEFAULT 0,
    avg_trust_score FLOAT DEFAULT 0.0,
    ops_pending     INTEGER DEFAULT 0,
    ops_running     INTEGER DEFAULT 0,
    ops_failed_24h  INTEGER DEFAULT 0,
    ops_done_24h    INTEGER DEFAULT 0,
    proxies_healthy INTEGER DEFAULT 0,
    proxies_total   INTEGER DEFAULT 0,
    active_alerts   INTEGER DEFAULT 0,
    active_anomalies INTEGER DEFAULT 0,
    active_recoveries INTEGER DEFAULT 0,
    components      JSONB DEFAULT '{}',    -- {account: score, proxy: score, queue: score}
    snapshot_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_health_snapshots_owner ON system_health_snapshots(owner_id, snapshot_at DESC);
-- Keep only last 7 days of snapshots (purged by db_maintenance)
