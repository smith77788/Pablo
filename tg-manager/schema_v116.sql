-- schema_v116: Semantic Memory CRM — per-user per-bot conversation memory

CREATE TABLE IF NOT EXISTS bot_user_memory (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    role VARCHAR(8) NOT NULL,  -- 'user' or 'bot'
    text TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_user_facts (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    fact_key VARCHAR(64) NOT NULL,  -- 'name', 'interests', 'purchases', 'pain_points', etc.
    fact_value TEXT NOT NULL,
    confidence FLOAT DEFAULT 1.0,
    extracted_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(bot_id, user_id, fact_key)
);

CREATE TABLE IF NOT EXISTS memory_settings (
    bot_id BIGINT PRIMARY KEY,
    enabled BOOLEAN DEFAULT TRUE,
    max_history_days INT DEFAULT 90,
    auto_extract_facts BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_user_memory_lookup ON bot_user_memory(bot_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_user_facts_lookup ON bot_user_facts(bot_id, user_id);
