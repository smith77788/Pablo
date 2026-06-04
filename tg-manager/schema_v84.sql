-- schema_v84: activity_log — лог всех действий пользователей в боте
-- Источник: UserActivityLogMiddleware (команды + кнопки + ошибки)
CREATE TABLE IF NOT EXISTS activity_log (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    event_type  TEXT NOT NULL,              -- 'command', 'callback', 'message', 'error'
    action      TEXT NOT NULL,              -- '/start', 'chan:menu', fsm_state, etc.
    detail      TEXT,                       -- безопасный дополнительный контекст
    status      TEXT NOT NULL DEFAULT 'ok', -- 'ok', 'error'
    error_msg   TEXT,
    duration_ms INT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_activity_log_owner  ON activity_log(owner_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_recent ON activity_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_errors ON activity_log(status, occurred_at DESC) WHERE status = 'error';
