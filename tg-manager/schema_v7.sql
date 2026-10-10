-- Schema v7: Swarm routing system

-- Bot roles and swarm config
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS bot_role TEXT NOT NULL DEFAULT 'general'
    CHECK (bot_role IN ('entry', 'conversion', 'retention', 'general'));
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS cluster TEXT DEFAULT 'default';
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS swarm_weight FLOAT NOT NULL DEFAULT 1.0;
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS swarm_enabled BOOLEAN NOT NULL DEFAULT false;

-- System mode (one row = global mode)
CREATE TABLE IF NOT EXISTS system_mode (
    id INTEGER PRIMARY KEY DEFAULT 1,
    mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (mode IN ('manual', 'assisted', 'autopilot', 'growth', 'experiment', 'stability')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO system_mode (id, mode) VALUES (1, 'manual') ON CONFLICT (id) DO NOTHING;

-- Bot performance metrics (updated by analytics)
CREATE TABLE IF NOT EXISTS bot_metrics (
    bot_id BIGINT PRIMARY KEY REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    ctr FLOAT NOT NULL DEFAULT 0,
    conversion_rate FLOAT NOT NULL DEFAULT 0,
    retention_d1 FLOAT NOT NULL DEFAULT 0,
    retention_d7 FLOAT NOT NULL DEFAULT 0,
    score FLOAT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Routing rules (which bot handles which traffic)
CREATE TABLE IF NOT EXISTS routing_rules (
    id SERIAL PRIMARY KEY,
    source_bot_id BIGINT REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    target_bot_id BIGINT REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    trigger_event TEXT NOT NULL, -- 'start', 'keyword', 'auto'
    keyword TEXT,
    weight FLOAT NOT NULL DEFAULT 1.0,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
