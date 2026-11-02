-- schema_v275: коннекторы amoCRM и Bitrix24.
--
-- CRM — ещё один вид получателя событий (webhook_endpoints.kind), а не
-- отдельная подсистема: у получателя уже есть очередь, повторы с паузой,
-- журнал доставок, выключение после серии сбоев и экран. Отдельная очередь
-- для CRM означала бы второй набор этих правил.
--
-- Что добавлено:
--   1. kind 'amocrm' | 'bitrix24', настройки подключения (crm_config: адрес,
--      воронка, соответствие этапов), учётные данные — зашифрованно в
--      secret_enc, как секрет подписи у http-получателя.
--   2. inbound_token — секрет в адресе входящего вебхука CRM → Infragram
--      (смена этапа сделки в CRM меняет этап у нас).
--   3. crm_links — какая запись у нас стала какой записью в CRM. Без неё
--      каждое событие по человеку заводило бы в CRM нового.
--   4. События воронки контактов (pipeline.updated) и простых сделок
--      (deal.updated) — триггерами, потому что стадию пишут несколько дверей
--      (экран контакта, канбан, сенсор намерений, бот, сценарий).

ALTER TABLE webhook_endpoints DROP CONSTRAINT IF EXISTS webhook_endpoints_kind_chk;
ALTER TABLE webhook_endpoints ADD CONSTRAINT webhook_endpoints_kind_chk
    CHECK (kind IN ('http', 'sheets', 'amocrm', 'bitrix24'));

ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS crm_config JSONB NOT NULL DEFAULT '{}';
ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS inbound_token TEXT;
-- Bitrix24 подписывает исходящий вебхук своим application_token: храним
-- зашифрованно и сверяем, чтобы чужой запрос на наш адрес не менял этапы.
ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS inbound_secret_enc TEXT;
ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_endpoints_inbound_token
    ON webhook_endpoints(inbound_token) WHERE inbound_token IS NOT NULL;
-- Одна CRM каждого вида на владельца: два подключения к одной amoCRM
-- завели бы каждого человека дважды.
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_endpoints_owner_crm
    ON webhook_endpoints(owner_id, kind) WHERE kind IN ('amocrm', 'bitrix24');

-- local_key: 'contact:<uuid>' (воронка контактов), 'deal:<id>' (crm_deals),
-- 'chat:<bot_id>:<chat_id>' (человек из бота), 'order:<id>' (заказ ИИ-продавца).
CREATE TABLE IF NOT EXISTS crm_links (
    endpoint_id    BIGINT NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
    local_key      TEXT   NOT NULL,
    owner_id       BIGINT NOT NULL,
    contact_ext_id TEXT,
    deal_ext_id    TEXT,
    -- Этап и сумма, о которых CRM и мы уже договорились. Событие с тем же
    -- этапом и суммой в CRM не отправляется — так смена этапа, пришедшая из
    -- CRM, не уходит обратно эхом.
    last_stage     TEXT,
    last_price     NUMERIC(14, 2),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (endpoint_id, local_key)
);
CREATE INDEX IF NOT EXISTS idx_crm_links_deal
    ON crm_links(endpoint_id, deal_ext_id) WHERE deal_ext_id IS NOT NULL;

-- ── pipeline.updated: контакт в воронке появился, сменил этап или сумму ────
CREATE OR REPLACE FUNCTION _integration_emit_pipeline() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.stage IS DISTINCT FROM OLD.stage
       OR NEW.deal_value IS DISTINCT FROM OLD.deal_value
       OR NEW.currency IS DISTINCT FROM OLD.currency THEN
        BEGIN
            IF EXISTS (SELECT 1 FROM webhook_endpoints
                       WHERE owner_id = NEW.owner_id AND is_active
                         AND (cardinality(events) = 0 OR 'pipeline.updated' = ANY(events))) THEN
                INSERT INTO integration_events (owner_id, event, payload)
                VALUES (NEW.owner_id, 'pipeline.updated', jsonb_strip_nulls(jsonb_build_object(
                    'contact_id', NEW.contact_id,
                    'stage', COALESCE(NEW.stage, 'lead'),
                    'old_stage', CASE WHEN TG_OP = 'UPDATE' THEN OLD.stage END,
                    'deal_value', NEW.deal_value,
                    'currency', COALESCE(NEW.currency, 'USD'),
                    'created', TG_OP = 'INSERT')));
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'integration event (contact_crm %) skipped: %', NEW.id, SQLERRM;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_pipeline ON contact_crm;
CREATE TRIGGER trg_integration_emit_pipeline
    AFTER INSERT OR UPDATE OF stage, deal_value, currency ON contact_crm
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_pipeline();

-- ── deal.updated: простая сделка (crm_deals) создана или изменилась ───────
CREATE OR REPLACE FUNCTION _integration_emit_deal() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.stage IS DISTINCT FROM OLD.stage
       OR NEW.value IS DISTINCT FROM OLD.value
       OR NEW.title IS DISTINCT FROM OLD.title THEN
        BEGIN
            IF EXISTS (SELECT 1 FROM webhook_endpoints
                       WHERE owner_id = NEW.owner_id AND is_active
                         AND (cardinality(events) = 0 OR 'deal.updated' = ANY(events))) THEN
                INSERT INTO integration_events (owner_id, event, payload)
                VALUES (NEW.owner_id, 'deal.updated', jsonb_strip_nulls(jsonb_build_object(
                    'deal_id', NEW.id,
                    'title', NEW.title,
                    'contact', NEW.contact,
                    'stage', NEW.stage,
                    'old_stage', CASE WHEN TG_OP = 'UPDATE' THEN OLD.stage END,
                    'value', NEW.value,
                    'created', TG_OP = 'INSERT')));
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'integration event (crm_deals %) skipped: %', NEW.id, SQLERRM;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_deal ON crm_deals;
CREATE TRIGGER trg_integration_emit_deal
    AFTER INSERT OR UPDATE OF stage, value, title ON crm_deals
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_deal();
