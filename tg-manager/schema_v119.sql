-- schema_v119: Audience DNA — behavioral profiling and content analytics

CREATE TABLE IF NOT EXISTS audience_dna (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    peak_hours INT[] DEFAULT ARRAY[]::INT[],
    peak_days TEXT[] DEFAULT ARRAY[]::TEXT[],
    best_content_types TEXT[] DEFAULT ARRAY[]::TEXT[],
    avg_engagement_rate FLOAT DEFAULT 0,
    churn_risk_pct FLOAT DEFAULT 0,
    top_topics TEXT[] DEFAULT ARRAY[]::TEXT[],
    total_users_analyzed INT DEFAULT 0,
    computed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS content_performance (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    message_id BIGINT,
    content_type VARCHAR(32),
    views INT DEFAULT 0,
    reactions INT DEFAULT 0,
    forwards INT DEFAULT 0,
    replies INT DEFAULT 0,
    publish_hour INT,
    publish_weekday INT,
    engagement_rate FLOAT GENERATED ALWAYS AS (
        CASE WHEN views > 0 THEN (reactions + forwards)::FLOAT / views ELSE 0 END
    ) STORED,
    published_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audience_dna_bot ON audience_dna(bot_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_perf_bot ON content_performance(bot_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_perf_engagement ON content_performance(bot_id, engagement_rate DESC);
