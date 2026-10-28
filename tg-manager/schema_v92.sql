-- schema_v92: auto_reply_log table for tracking auto-reply fires

CREATE TABLE IF NOT EXISTS auto_reply_log (
    id         BIGSERIAL PRIMARY KEY,
    bot_id     BIGINT    NOT NULL,
    chat_id    BIGINT    NOT NULL,
    rule_id    INTEGER,
    rule_type  TEXT      NOT NULL DEFAULT 'auto_reply',  -- 'auto_reply' | 'automation'
    trigger_type TEXT,
    keyword    TEXT,
    fired_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auto_reply_log_bot_id
    ON auto_reply_log(bot_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_auto_reply_log_chat_id
    ON auto_reply_log(bot_id, chat_id, fired_at DESC);
