-- v194: журнал срабатываний сенсора намерений.
--
-- Причина: у правила был только счётчик hits — голое число без деталей. Понять,
-- НА ЧТО именно сработало правило, было негде, поэтому ложные срабатывания
-- (классический пример — фраза «готов», ловившая «не готов») оставались
-- невидимыми: контакт молча уезжал не в ту стадию, а оператор видел лишь
-- растущий счётчик. Без журнала правила невозможно отлаживать.
CREATE TABLE IF NOT EXISTS vault_intent_hits (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    rule_id     BIGINT NOT NULL REFERENCES vault_intent_rules(id) ON DELETE CASCADE,
    chat_id     BIGINT,
    peer_name   TEXT,
    -- Фрагмент сообщения, на котором сработало правило. Хранится в открытом
    -- виде осознанно: это короткая выдержка для отладки правил, доступная
    -- только владельцу, и шифровать её ради превью в списке смысла нет.
    text_preview TEXT,
    stage_to    TEXT,          -- куда двинули стадию (или NULL — не двигали)
    tags_added  TEXT[],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intent_hits_owner_time
    ON vault_intent_hits(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intent_hits_rule
    ON vault_intent_hits(rule_id, created_at DESC);
