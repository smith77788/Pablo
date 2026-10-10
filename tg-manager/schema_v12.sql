-- Missing tables for engagement, SEO, network routing, deeplink tracking

CREATE TABLE IF NOT EXISTS user_activity (
    id          SERIAL PRIMARY KEY,
    bot_id      BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 1,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_user_activity_bot ON user_activity(bot_id);
CREATE INDEX IF NOT EXISTS idx_user_activity_last_seen ON user_activity(bot_id, last_seen);

CREATE TABLE IF NOT EXISTS keyword_stats (
    id          SERIAL PRIMARY KEY,
    bot_id      BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_keyword_stats_bot ON keyword_stats(bot_id, count DESC);

CREATE TABLE IF NOT EXISTS deep_link_visits (
    id          SERIAL PRIMARY KEY,
    link_id     INTEGER NOT NULL REFERENCES bot_deep_links(id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    visited_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (link_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_deep_link_visits_link ON deep_link_visits(link_id);

CREATE TABLE IF NOT EXISTS routing_log (
    id          SERIAL PRIMARY KEY,
    from_bot_id BIGINT NOT NULL,
    to_bot_id   BIGINT,
    user_id     BIGINT NOT NULL,
    decision    TEXT NOT NULL,
    system_mode TEXT,
    score_from  REAL,
    score_to    REAL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_routing_log_from_bot ON routing_log(from_bot_id, created_at);

CREATE TABLE IF NOT EXISTS bot_routing_weights (
    id          SERIAL PRIMARY KEY,
    bot_id      BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    weight      INTEGER NOT NULL DEFAULT 50,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id)
);
