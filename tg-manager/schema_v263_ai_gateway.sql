-- schema_v263: единый AI-шлюз (services/ai_gateway.py) и журнал ответов ИИ.
--
-- llm_owner_usage — расход ИИ по владельцу, задаче и дню. llm_usage (v239)
-- считает только по провайдерам: по нему не видно, кто тратит квоту, и нельзя
-- поставить лимит по тарифу.
CREATE TABLE IF NOT EXISTS llm_owner_usage (
    day DATE NOT NULL,
    owner_id BIGINT NOT NULL,
    task TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, owner_id, task)
);
CREATE INDEX IF NOT EXISTS idx_llm_owner_usage_owner_day
    ON llm_owner_usage (owner_id, day DESC);
-- Уборка старых дней (services/db_maintenance) идёт по day.
CREATE INDEX IF NOT EXISTS idx_llm_owner_usage_day ON llm_owner_usage (day);

-- ai_reply_log — что ИИ ответил клиенту и что с этим сделала проверка
-- (guard: price — названа цена не из прайса, discount — скидка сверх лимита,
-- injection — попытка переписать правила). rating ставит владелец: 1 хорошо,
-- -1 плохо. Без журнала качество ответов нечем измерить.
CREATE TABLE IF NOT EXISTS ai_reply_log (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    source TEXT NOT NULL,
    source_id BIGINT,
    provider TEXT,
    model TEXT,
    user_text TEXT NOT NULL DEFAULT '',
    reply TEXT NOT NULL DEFAULT '',
    guard TEXT,
    guard_detail TEXT,
    rating SMALLINT CHECK (rating IN (-1, 1)),
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_reply_log_owner_created
    ON ai_reply_log (owner_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_reply_log_created
    ON ai_reply_log (created_at);
