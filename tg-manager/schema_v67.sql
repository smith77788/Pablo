-- EPOCH III: Ecosystem Brain
-- Ecosystem tables for BotMother OS

CREATE TABLE IF NOT EXISTS ecosystems (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT DEFAULT '',
    ecosystem_type TEXT DEFAULT 'custom',
    status         TEXT DEFAULT 'active',
    health_score   FLOAT DEFAULT 1.0,
    stability_score FLOAT DEFAULT 1.0,
    reliability_score FLOAT DEFAULT 1.0,
    recovery_score  FLOAT DEFAULT 1.0,
    growth_score    FLOAT DEFAULT 0.0,
    pressure_score  INT DEFAULT 0,
    risk_level     TEXT DEFAULT 'low',
    region         TEXT,
    meta           JSONB DEFAULT '{}',
    dna_id         BIGINT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ecosystems_owner ON ecosystems(owner_id);
CREATE INDEX IF NOT EXISTS idx_ecosystems_status ON ecosystems(owner_id, status);

CREATE TABLE IF NOT EXISTS ecosystem_members (
    id           BIGSERIAL PRIMARY KEY,
    ecosystem_id BIGINT NOT NULL REFERENCES ecosystems(id) ON DELETE CASCADE,
    owner_id     BIGINT NOT NULL,
    object_type  TEXT NOT NULL,
    object_id    BIGINT NOT NULL,
    role         TEXT DEFAULT 'member',
    added_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ecosystem_id, object_type, object_id)
);
CREATE INDEX IF NOT EXISTS idx_eco_members_eco  ON ecosystem_members(ecosystem_id);
CREATE INDEX IF NOT EXISTS idx_eco_members_obj  ON ecosystem_members(owner_id, object_type, object_id);

CREATE TABLE IF NOT EXISTS ecosystem_events (
    id           BIGSERIAL PRIMARY KEY,
    ecosystem_id BIGINT NOT NULL,
    owner_id     BIGINT NOT NULL,
    event_type   TEXT NOT NULL,
    severity     TEXT DEFAULT 'info',
    title        TEXT NOT NULL,
    details      JSONB DEFAULT '{}',
    object_type  TEXT,
    object_id    BIGINT,
    occurred_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eco_events_eco ON ecosystem_events(ecosystem_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_eco_events_own ON ecosystem_events(owner_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS ecosystem_drift_log (
    id           BIGSERIAL PRIMARY KEY,
    ecosystem_id BIGINT NOT NULL,
    owner_id     BIGINT NOT NULL,
    drift_type   TEXT NOT NULL,
    object_type  TEXT,
    object_id    BIGINT,
    description  TEXT NOT NULL,
    suggested_fix TEXT,
    auto_fixable BOOLEAN DEFAULT FALSE,
    resolved     BOOLEAN DEFAULT FALSE,
    resolved_at  TIMESTAMPTZ,
    detected_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eco_drift_eco ON ecosystem_drift_log(ecosystem_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_eco_drift_unresolved ON ecosystem_drift_log(ecosystem_id, resolved);

CREATE TABLE IF NOT EXISTS ecosystem_dna (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    name          TEXT NOT NULL,
    dna_type      TEXT NOT NULL,
    description   TEXT DEFAULT '',
    template_data JSONB NOT NULL DEFAULT '{}',
    is_public     BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eco_dna_owner ON ecosystem_dna(owner_id);
