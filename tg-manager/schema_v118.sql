-- schema_v118: Stars Optimizer — A/B testing for Telegram Stars monetization

CREATE TABLE IF NOT EXISTS stars_experiments (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    name VARCHAR(128) NOT NULL,
    content_type VARCHAR(64) DEFAULT 'message',  -- message/media/subscription/gift
    price_a INT NOT NULL,
    price_b INT NOT NULL,
    impressions_a INT DEFAULT 0,
    conversions_a INT DEFAULT 0,
    revenue_a INT DEFAULT 0,  -- total Stars earned from variant A
    impressions_b INT DEFAULT 0,
    conversions_b INT DEFAULT 0,
    revenue_b INT DEFAULT 0,
    status VARCHAR(16) DEFAULT 'active',  -- active/paused/completed
    winner VARCHAR(2),  -- 'a' or 'b' or null
    significance FLOAT,  -- chi-square p-value
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS stars_events (
    id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT REFERENCES stars_experiments(id) ON DELETE CASCADE,
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    variant VARCHAR(2) NOT NULL,
    event_type VARCHAR(16) NOT NULL,  -- 'impression' or 'conversion'
    stars_amount INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stars_transactions (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    stars_amount INT NOT NULL,
    description TEXT,
    transaction_id VARCHAR(128),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stars_experiments_bot ON stars_experiments(bot_id, status);
CREATE INDEX IF NOT EXISTS idx_stars_events_exp ON stars_events(experiment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stars_transactions_bot ON stars_transactions(bot_id, created_at DESC);
