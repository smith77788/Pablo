-- Global Presence Factory: plans and per-city targets
CREATE TABLE IF NOT EXISTS global_presence_plans (
    id                SERIAL PRIMARY KEY,
    owner_id          BIGINT NOT NULL,
    asset_type        TEXT NOT NULL DEFAULT 'channel',   -- 'channel'|'group'|'bot'|'package'
    template_id       INT,
    name_pattern      TEXT NOT NULL,
    username_pattern  TEXT,
    geo_selection     JSONB NOT NULL DEFAULT '{}',
    account_selection JSONB NOT NULL DEFAULT '{}',
    safety_settings   JSONB NOT NULL DEFAULT '{"safe_mode": true}',
    status            TEXT NOT NULL DEFAULT 'draft',     -- draft|queued|running|done|failed|cancelled
    op_id             INT,                               -- operation_queue id once queued
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gpp_owner_status ON global_presence_plans(owner_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS global_presence_targets (
    id                  SERIAL PRIMARY KEY,
    plan_id             INT NOT NULL REFERENCES global_presence_plans(id) ON DELETE CASCADE,
    country             TEXT,
    country_code        TEXT,
    region              TEXT,
    city                TEXT,
    city_slug           TEXT,
    language            TEXT,
    timezone            TEXT,
    asset_type          TEXT NOT NULL DEFAULT 'channel',
    planned_name        TEXT,
    planned_username    TEXT,
    selected_account_id INT,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|skipped
    result_asset_id     BIGINT,
    error_message       TEXT,
    retryable           BOOLEAN DEFAULT true,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gpt_plan_status ON global_presence_targets(plan_id, status);
