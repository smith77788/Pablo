-- schema_v363: история стадий сделок.
--
-- Воронка знала только СНИМОК: сколько сделок сейчас в каждой стадии. Ни одна
-- таблица не помнила, когда сделка перешла из стадии в стадию, поэтому
-- конверсию между стадиями, время в стадии, долю выигранных, «застрявшие»
-- сделки и прогноз выручки посчитать было не из чего вообще — не «плохо», а
-- никак. У лидеров (Pipedrive, amoCRM, HubSpot) это базовые отчёты продаж.
--
-- Почему триггер, а не запись в коде. Стадию пишут несколько дверей: экран
-- контакта, канбан, сенсор намерений, сценарии, бот, входящий вебхук
-- amoCRM/Bitrix24 (schema_v275 по той же причине вешает события на триггер).
-- Запись в одной двери оставила бы историю дырявой ровно на остальных.
--
-- deal_kind: 'contact' — воронка контактов (contact_crm, ключ — contact_id),
--            'deal'    — простая сделка (crm_deals, ключ — id).
-- backfilled: строка восстановлена по уже существующим сделкам при накате;
--             время перехода в ней НЕ известно (взято время создания), поэтому
--             в длительности она не идёт, а в «дошла до стадии» — идёт.

CREATE TABLE IF NOT EXISTS crm_stage_history (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    deal_kind   TEXT        NOT NULL CHECK (deal_kind IN ('contact', 'deal')),
    deal_key    TEXT        NOT NULL,
    from_stage  TEXT,
    to_stage    TEXT        NOT NULL,
    deal_value  NUMERIC(14, 2),
    currency    TEXT,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    backfilled  BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_crm_stage_history_deal
    ON crm_stage_history(owner_id, deal_kind, deal_key, changed_at);
CREATE INDEX IF NOT EXISTS idx_crm_stage_history_owner_time
    ON crm_stage_history(owner_id, changed_at);

-- ── Воронка контактов ─────────────────────────────────────────────────────
-- Смена contact_id — это слияние дублей (merge_engine._absorb_crm перецепляет
-- сделку второго контакта на основной): история едет вместе со сделкой, иначе
-- у основного контакта сделка была бы «без прошлого».
CREATE OR REPLACE FUNCTION _crm_stage_history_contact() RETURNS trigger AS $$
BEGIN
    BEGIN
        IF TG_OP = 'UPDATE' AND NEW.contact_id IS DISTINCT FROM OLD.contact_id THEN
            UPDATE crm_stage_history SET deal_key = NEW.contact_id::text
             WHERE owner_id = NEW.owner_id AND deal_kind = 'contact'
               AND deal_key = OLD.contact_id::text;
        END IF;
        IF TG_OP = 'INSERT' OR NEW.stage IS DISTINCT FROM OLD.stage THEN
            INSERT INTO crm_stage_history
                (owner_id, deal_kind, deal_key, from_stage, to_stage, deal_value, currency)
            VALUES (NEW.owner_id, 'contact', NEW.contact_id::text,
                    CASE WHEN TG_OP = 'UPDATE' THEN COALESCE(OLD.stage, 'lead') END,
                    COALESCE(NEW.stage, 'lead'), NEW.deal_value, COALESCE(NEW.currency, 'USD'));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        -- История не имеет права ронять смену стадии: стадия важнее отчёта.
        RAISE WARNING 'crm_stage_history (contact_crm %) skipped: %', NEW.id, SQLERRM;
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_crm_stage_history_contact ON contact_crm;
CREATE TRIGGER trg_crm_stage_history_contact
    AFTER INSERT OR UPDATE OF stage, contact_id ON contact_crm
    FOR EACH ROW EXECUTE FUNCTION _crm_stage_history_contact();

-- ── Простые сделки ────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION _crm_stage_history_deal() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.stage IS DISTINCT FROM OLD.stage THEN
        BEGIN
            INSERT INTO crm_stage_history
                (owner_id, deal_kind, deal_key, from_stage, to_stage, deal_value, currency)
            VALUES (NEW.owner_id, 'deal', NEW.id::text,
                    CASE WHEN TG_OP = 'UPDATE' THEN COALESCE(OLD.stage, 'lead') END,
                    COALESCE(NEW.stage, 'lead'), NEW.value, NEW.currency);
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'crm_stage_history (crm_deals %) skipped: %', NEW.id, SQLERRM;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_crm_stage_history_deal ON crm_deals;
CREATE TRIGGER trg_crm_stage_history_deal
    AFTER INSERT OR UPDATE OF stage ON crm_deals
    FOR EACH ROW EXECUTE FUNCTION _crm_stage_history_deal();

-- ── Восстановление по уже существующим сделкам ─────────────────────────────
-- Одна строка на сделку: «появилась в текущей стадии». Повторный накат ничего
-- не задваивает (NOT EXISTS).
INSERT INTO crm_stage_history
    (owner_id, deal_kind, deal_key, from_stage, to_stage, deal_value, currency,
     changed_at, backfilled)
SELECT c.owner_id, 'contact', c.contact_id::text, NULL, COALESCE(c.stage, 'lead'),
       c.deal_value, COALESCE(c.currency, 'USD'), COALESCE(c.created_at, now()), TRUE
  FROM contact_crm c
 WHERE NOT EXISTS (SELECT 1 FROM crm_stage_history h
                    WHERE h.owner_id = c.owner_id AND h.deal_kind = 'contact'
                      AND h.deal_key = c.contact_id::text);

INSERT INTO crm_stage_history
    (owner_id, deal_kind, deal_key, from_stage, to_stage, deal_value, currency,
     changed_at, backfilled)
SELECT d.owner_id, 'deal', d.id::text, NULL, COALESCE(d.stage, 'lead'),
       d.value, d.currency, COALESCE(d.created_at, now()), TRUE
  FROM crm_deals d
 WHERE NOT EXISTS (SELECT 1 FROM crm_stage_history h
                    WHERE h.owner_id = d.owner_id AND h.deal_kind = 'deal'
                      AND h.deal_key = d.id::text);
