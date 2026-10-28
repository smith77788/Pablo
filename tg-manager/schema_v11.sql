CREATE TABLE IF NOT EXISTS bot_deep_links (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    start_param TEXT NOT NULL,
    click_count INTEGER NOT NULL DEFAULT 0,
    unique_users INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, start_param)
);
CREATE INDEX IF NOT EXISTS idx_deep_links_bot ON bot_deep_links(bot_id);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    referrer_user_id BIGINT NOT NULL,
    referred_user_id BIGINT NOT NULL,
    deep_link_id INTEGER REFERENCES bot_deep_links(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, referred_user_id)
);
CREATE INDEX IF NOT EXISTS idx_referrals_bot_referrer ON referrals(bot_id, referrer_user_id);
