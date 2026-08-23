-- schema_v179: Разогрев ЛЮБОГО чата флотом с осмысленным LLM-диалогом.
-- Флот вступает в указанный чат и ведёт естественную беседу: между собой по
-- темам (seed) и/или реагируя на реальных участников по контексту (engage).
-- Управляется фоновым циклом chat_warmup.run (как ghost_engine).

CREATE TABLE IF NOT EXISTS chat_warmup_sessions (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    chat_ref        TEXT   NOT NULL,             -- как ввёл пользователь: @username / ссылка / id
    chat_id         BIGINT,                      -- разрезолвленный peer id (после первого коннекта)
    account_ids     BIGINT[] NOT NULL DEFAULT '{}',
    mode            TEXT   NOT NULL DEFAULT 'mixed',   -- seed | engage | mixed
    topics          TEXT,                        -- темы через запятую (пусто = любые)
    intensity       TEXT   NOT NULL DEFAULT 'normal',  -- calm | normal | active
    status          TEXT   NOT NULL DEFAULT 'active',  -- active | paused | stopped
    last_run_at     TIMESTAMPTZ,
    last_speaker    BIGINT,                      -- id аккаунта, говорившего последним (анти-двойник)
    last_seen_msg   BIGINT NOT NULL DEFAULT 0,   -- id последнего обработанного внешнего сообщения
    messages_sent   INT    NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_warmup_owner_status
    ON chat_warmup_sessions(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_chat_warmup_active
    ON chat_warmup_sessions(status) WHERE status = 'active';
