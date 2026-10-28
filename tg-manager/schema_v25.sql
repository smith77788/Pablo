-- schema_v25.sql
-- Extend automation_rules with new trigger/action types
-- (no ALTER needed — trigger_type/action_type are already TEXT)
-- Add inactivity sweep log to prevent duplicate alerts
CREATE TABLE IF NOT EXISTS inactivity_alerts_sent (
    id          BIGSERIAL PRIMARY KEY,
    bot_id      BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    rule_id     BIGINT NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(bot_id, chat_id, rule_id)
);
