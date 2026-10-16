-- Visibility Engine: keyword tracking, position history, alerts
-- schema_v29

-- Ключевые слова для отслеживания (Visibility Engine)
CREATE TABLE IF NOT EXISTS search_keywords (
    id          SERIAL PRIMARY KEY,
    bot_id      INT NOT NULL,
    owner_id    BIGINT NOT NULL,
    keyword     TEXT NOT NULL,
    region      TEXT DEFAULT 'all',
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bot_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_search_kw_bot ON search_keywords(bot_id);
CREATE INDEX IF NOT EXISTS idx_search_kw_owner ON search_keywords(owner_id);

-- История позиций по ключевым словам
CREATE TABLE IF NOT EXISTS position_history (
    id          SERIAL PRIMARY KEY,
    bot_id      INT NOT NULL,
    keyword     TEXT NOT NULL,
    position    INT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pos_hist_bot ON position_history(bot_id, keyword, checked_at DESC);

-- Настройки алертов видимости
CREATE TABLE IF NOT EXISTS visibility_alert_settings (
    owner_id         BIGINT PRIMARY KEY,
    drop_threshold   INT DEFAULT 10,
    rise_threshold   INT DEFAULT 5,
    alerts_enabled   BOOLEAN DEFAULT true
);
