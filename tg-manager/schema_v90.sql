-- schema_v90: CRM deal pipeline, SEO score history, topology influence

-- ── CRM Deals (pipeline) ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_deals (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    title       TEXT NOT NULL,
    contact     TEXT,
    stage       TEXT NOT NULL DEFAULT 'new'
                    CHECK (stage IN ('new','contacted','qualified','won','lost')),
    value       NUMERIC(14,2) DEFAULT 0,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_deals_owner ON crm_deals(owner_id);
CREATE INDEX IF NOT EXISTS idx_crm_deals_stage ON crm_deals(owner_id, stage);

-- ── CRM Activity log ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_activity (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    deal_id     INTEGER REFERENCES crm_deals(id) ON DELETE CASCADE,
    note        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_activity_deal ON crm_activity(deal_id);

-- ── SEO score history ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS seo_score_history (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'bot',
    entity_id   BIGINT NOT NULL,
    score       INTEGER NOT NULL,
    tips_json   TEXT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_seo_score_history_entity
    ON seo_score_history(owner_id, entity_type, entity_id, checked_at DESC);
