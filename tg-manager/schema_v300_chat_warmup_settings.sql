-- schema_v300: настройки разогрева чата, которых не хватало.
--
-- У сессии разогрева было четыре настройки: чат, режим, темы, интенсивность.
-- Язык реплик был жёстко русским, у флота не было ни часов работы, ни лимита
-- реплик в сутки (ни на чат, ни на аккаунт), ни срока: сессия жила, пока её
-- не остановят руками, а поправить запущенную было нельзя вовсе.
--
--   lang              — язык реплик: ru | uk | en | auto (как пишут в чате);
--   active_from/to    — часы работы по поясу владельца [from, to); NULL — всегда;
--   daily_limit       — реплик в сутки на весь чат (0 — без лимита);
--   per_account_daily — реплик в сутки на один аккаунт (0 — без лимита);
--   reply_len         — длина реплик: short (1–2 фразы) | medium (2–4 фразы);
--   react_pct         — доля ответов участникам реакцией вместо текста, %;
--   ends_at           — автостоп по времени (NULL — без срока);
--   max_messages      — автостоп после N реплик (0 — без потолка).
ALTER TABLE chat_warmup_sessions
    ADD COLUMN IF NOT EXISTS lang              TEXT     NOT NULL DEFAULT 'ru',
    ADD COLUMN IF NOT EXISTS active_from       SMALLINT CHECK (active_from BETWEEN 0 AND 23),
    ADD COLUMN IF NOT EXISTS active_to         SMALLINT CHECK (active_to BETWEEN 0 AND 23),
    ADD COLUMN IF NOT EXISTS daily_limit       INT      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS per_account_daily INT      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reply_len         TEXT     NOT NULL DEFAULT 'short',
    ADD COLUMN IF NOT EXISTS react_pct         SMALLINT NOT NULL DEFAULT 28
        CHECK (react_pct BETWEEN 0 AND 60),
    ADD COLUMN IF NOT EXISTS ends_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS max_messages      INT      NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_chat_warmup_log_session_ok
    ON chat_warmup_log(session_id, created_at) WHERE status = 'ok';
