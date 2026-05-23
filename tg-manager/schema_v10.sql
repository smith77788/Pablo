-- A/B Experiments
CREATE TABLE IF NOT EXISTS experiments (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    experiment_type TEXT NOT NULL DEFAULT 'start_message'
        CHECK (experiment_type IN ('start_message', 'auto_reply', 'funnel')),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'paused', 'completed')),
    winner_variant_id INTEGER,
    min_sample_size INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS experiment_variants (
    id SERIAL PRIMARY KEY,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 50,
    impressions INTEGER NOT NULL DEFAULT 0,
    conversions INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS experiment_assignments (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    variant_id INTEGER NOT NULL REFERENCES experiment_variants(id) ON DELETE CASCADE,
    converted BOOLEAN NOT NULL DEFAULT false,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, user_id, experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_exp_assignments_bot_user ON experiment_assignments(bot_id, user_id);
