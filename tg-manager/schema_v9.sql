-- User tags (CRM)
CREATE TABLE IF NOT EXISTS user_tags (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    tag TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, user_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_user_tags_bot_user ON user_tags(bot_id, user_id);
CREATE INDEX IF NOT EXISTS idx_user_tags_bot_tag ON user_tags(bot_id, tag);

-- Automation rules
CREATE TABLE IF NOT EXISTS automation_rules (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('message_received', 'user_joined', 'tag_added', 'keyword')),
    trigger_value TEXT,  -- keyword for keyword trigger, tag name for tag_added
    action_type TEXT NOT NULL CHECK (action_type IN ('send_message', 'add_tag', 'remove_tag', 'subscribe_funnel')),
    action_value TEXT NOT NULL,  -- message text, tag name, or funnel_id
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
