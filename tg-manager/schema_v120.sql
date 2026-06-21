-- schema_v120: Ad Intelligence — Telegram advertising market database

CREATE TABLE IF NOT EXISTS ad_placements (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    channel_username VARCHAR(128) NOT NULL,
    channel_title VARCHAR(256),
    subscribers INT DEFAULT 0,
    views_avg INT DEFAULT 0,
    er_rate FLOAT DEFAULT 0,
    ad_price_est INT DEFAULT 0,  -- estimated price in Stars or USD cents
    quality_score FLOAT DEFAULT 0,  -- 0-100
    niches TEXT[] DEFAULT ARRAY[]::TEXT[],
    ad_posts_count INT DEFAULT 0,
    last_ad_seen_at TIMESTAMPTZ,
    last_scanned_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, channel_username)
);

CREATE TABLE IF NOT EXISTS ad_posts_log (
    id BIGSERIAL PRIMARY KEY,
    placement_id BIGINT REFERENCES ad_placements(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    advertiser_username VARCHAR(128),
    post_text TEXT,
    post_views INT DEFAULT 0,
    detected_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ad_advertisers (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    advertiser_username VARCHAR(128) NOT NULL,
    niche VARCHAR(64),
    placements_count INT DEFAULT 0,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, advertiser_username)
);

CREATE INDEX IF NOT EXISTS idx_ad_placements_owner_score ON ad_placements(owner_id, quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_ad_placements_niche ON ad_placements USING GIN(niches);
CREATE INDEX IF NOT EXISTS idx_ad_posts_placement ON ad_posts_log(placement_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ad_advertisers_owner_seen ON ad_advertisers(owner_id, last_seen_at DESC);
