-- Сенсор намерений: правила «ключевая фраза во входящем ЛС → CRM-стадия + тег +
-- алерт». Превращает Хранилище (Vault) из архива в триггер на граф контактов.
CREATE TABLE IF NOT EXISTS vault_intent_rules (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    phrase      TEXT NOT NULL,                     -- подстрока (матч в нижнем регистре)
    stage       TEXT,                              -- CRM-стадия назначения (или NULL)
    tag         TEXT,                              -- тег на контакт (или NULL)
    notify      BOOLEAN NOT NULL DEFAULT TRUE,     -- слать алерт владельцу
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    hits        INT NOT NULL DEFAULT 0,            -- сколько раз сработало
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vault_intent_owner
    ON vault_intent_rules(owner_id, is_active);
