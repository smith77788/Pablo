-- schema_v270: события сценариев (automation_events, schema_v268) — во вебхуки
-- и ленты Google Sheets (integration_events, schema_v262).
--
-- Шина сценариев пишет каждое событие у каждого владельца; outbox вебхуков
-- получает копию, только если у владельца есть активный получатель, ПОДПИСАННЫЙ
-- на этот тип (или на все). «Человек написал боту» — самое частое событие
-- продукта, и без проверки подписки оно копировалось бы у всех, у кого есть
-- хоть один вебхук на что-то другое.
--
-- webhook.received не пересылается: входящий вебхук, отправленный наружу
-- исходящим, при адресе получателя = адресу входа замкнул бы петлю.
CREATE OR REPLACE FUNCTION _integration_emit_automation() RETURNS trigger AS $$
BEGIN
    IF NEW.event_type <> 'webhook.received' THEN
        BEGIN
            IF EXISTS (SELECT 1 FROM webhook_endpoints
                       WHERE owner_id = NEW.owner_id AND is_active
                         AND (cardinality(events) = 0 OR NEW.event_type = ANY(events))) THEN
                INSERT INTO integration_events (owner_id, event, payload)
                VALUES (NEW.owner_id, NEW.event_type,
                        COALESCE(NEW.data, '{}'::jsonb)
                        || jsonb_strip_nulls(jsonb_build_object(
                               'bot_id', NEW.bot_id, 'chat_id', NEW.chat_id,
                               'automation_event_id', NEW.id)));
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'integration event (automation %) skipped: %', NEW.id, SQLERRM;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_automation ON automation_events;
CREATE TRIGGER trg_integration_emit_automation
    AFTER INSERT ON automation_events
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_automation();
