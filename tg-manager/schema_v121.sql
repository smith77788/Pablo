-- schema_v121: Narrative Engine — coordinated cross-network content campaigns

CREATE TABLE IF NOT EXISTS narrative_campaigns (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    topic VARCHAR(256) NOT NULL,
    core_message TEXT NOT NULL,
    campaign_type VARCHAR(32) DEFAULT 'trend',  -- trend/launch/awareness/counter
    spread_hours INT DEFAULT 4,
    posts_total INT DEFAULT 0,
    posts_published INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'draft',  -- draft/active/paused/completed/cancelled
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS narrative_posts (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT REFERENCES narrative_campaigns(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    channel_username VARCHAR(128) NOT NULL,
    bot_id BIGINT,
    angle VARCHAR(64),  -- news/expert/story/stats/opinion/question
    content TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'pending',  -- pending/published/failed/cancelled
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_narrative_campaigns_owner ON narrative_campaigns(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_narrative_posts_scheduled ON narrative_posts(scheduled_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_narrative_posts_campaign ON narrative_posts(campaign_id, status);
