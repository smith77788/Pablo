-- schema_v262: интеграции — исходящие вебхуки, события, Google Sheets, области API-ключей
--
-- Три вещи, которые нужны внешней системе, чтобы жить рядом с Infragram:
--   1. знать, что случилось (вебхуки на события) — без опроса API;
--   2. ходить в API своим ключом, а не общим ADMIN_SECRET (api_keys.scopes);
--   3. получать данные в Google Sheets — выгрузкой и лентой событий.

-- ── API-ключи: области доступа ──────────────────────────────────────────────
-- 'read'  — чтение (аккаунты, операции, переписка);
-- 'write' — действия от имени аккаунтов (отправка, нажатие кнопки).
-- Старые ключи получают обе области: до этой миграции ключ ничего не умел вовсе,
-- урезать то, что владелец только что получил, смысла нет.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scopes TEXT[] NOT NULL DEFAULT ARRAY['read','write'];
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

-- ── Получатели событий ──────────────────────────────────────────────────────
-- kind='http'   — POST JSON на url, подпись HMAC-SHA256 секретом secret_enc;
-- kind='sheets' — строка в лист sheet_name таблицы spreadsheet_id через
--                 сервисный аккаунт владельца (sheets_connections).
-- events пустой — подписка на все события.
CREATE TABLE IF NOT EXISTS webhook_endpoints (
    id                   BIGSERIAL PRIMARY KEY,
    owner_id             BIGINT NOT NULL,
    kind                 TEXT NOT NULL DEFAULT 'http',
    name                 TEXT NOT NULL DEFAULT '',
    url                  TEXT,
    spreadsheet_id       TEXT,
    sheet_name           TEXT,
    secret_enc           TEXT,
    events               TEXT[] NOT NULL DEFAULT '{}',
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    disabled_reason      TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    last_success_at      TIMESTAMPTZ,
    last_failure_at      TIMESTAMPTZ,
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT webhook_endpoints_kind_chk CHECK (kind IN ('http', 'sheets'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_owner_active
    ON webhook_endpoints(owner_id) WHERE is_active;

-- ── Исходящие события (outbox) ──────────────────────────────────────────────
-- Пишут сюда триггеры ниже, а не код исполнителя: операция закрывается в
-- десятке мест op_worker'а, и хук в одном из них молча пропускал бы остальные.
-- Строка появляется, только если у владельца есть хотя бы один активный
-- получатель, — у тех, кто интеграциями не пользуется, таблица не растёт.
CREATE TABLE IF NOT EXISTS integration_events (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    event         TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    fanned_out_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_integration_events_unfanned
    ON integration_events(id) WHERE fanned_out_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_integration_events_created
    ON integration_events(created_at);

-- ── Доставки: событие × получатель ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id               BIGSERIAL PRIMARY KEY,
    endpoint_id      BIGINT NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
    event_id         BIGINT NOT NULL REFERENCES integration_events(id) ON DELETE CASCADE,
    owner_id         BIGINT NOT NULL,
    event            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INT NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_status_code INT,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at     TIMESTAMPTZ,
    CONSTRAINT webhook_deliveries_once UNIQUE (endpoint_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
    ON webhook_deliveries(next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint
    ON webhook_deliveries(endpoint_id, created_at DESC);

-- ── Google Sheets: сервисный аккаунт владельца ──────────────────────────────
-- credentials_enc — JSON-ключ сервисного аккаунта, зашифрованный token_vault.
CREATE TABLE IF NOT EXISTS sheets_connections (
    owner_id        BIGINT PRIMARY KEY,
    client_email    TEXT NOT NULL,
    credentials_enc TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Триггеры-источники событий ──────────────────────────────────────────────
-- Каждый обёрнут в EXCEPTION: сбой интеграции НЕ ДОЛЖЕН откатывать закрытие
-- операции или смену статуса аккаунта — это дороже потерянного события.

-- Операция пришла в конечный статус: operation.done / partial / failed / cancelled.
CREATE OR REPLACE FUNCTION _integration_emit_operation() RETURNS trigger AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NEW.status IN ('done', 'partial', 'failed', 'cancelled') THEN
        BEGIN
            IF EXISTS (SELECT 1 FROM webhook_endpoints
                       WHERE owner_id = NEW.owner_id AND is_active) THEN
                INSERT INTO integration_events (owner_id, event, payload)
                VALUES (NEW.owner_id, 'operation.' || NEW.status, jsonb_build_object(
                    'operation_id', NEW.id,
                    'op_type', NEW.op_type,
                    'status', NEW.status,
                    'total_items', NEW.total_items,
                    'done_items', NEW.done_items,
                    'error', NEW.error_msg,
                    'created_at', NEW.created_at,
                    'finished_at', COALESCE(NEW.finished_at, now())));
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'integration event (operation %) skipped: %', NEW.id, SQLERRM;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_operation ON operation_queue;
CREATE TRIGGER trg_integration_emit_operation
    AFTER UPDATE OF status ON operation_queue
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_operation();

-- Аккаунт добавлен: account.added.
CREATE OR REPLACE FUNCTION _integration_emit_account_added() RETURNS trigger AS $$
BEGIN
    BEGIN
        IF EXISTS (SELECT 1 FROM webhook_endpoints
                   WHERE owner_id = NEW.owner_id AND is_active) THEN
            INSERT INTO integration_events (owner_id, event, payload)
            VALUES (NEW.owner_id, 'account.added', jsonb_build_object(
                'account_id', NEW.id,
                'phone', NEW.phone,
                'username', NEW.username,
                'status', COALESCE(NEW.acc_status, 'ok')));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'integration event (account %) skipped: %', NEW.id, SQLERRM;
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_account_added ON tg_accounts;
CREATE TRIGGER trg_integration_emit_account_added
    AFTER INSERT ON tg_accounts
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_account_added();

-- Смена статуса аккаунта: account.lost (бан/спамблок/заморозка/потеря сессии)
-- или account.status_changed (всё остальное, включая возврат в строй).
CREATE OR REPLACE FUNCTION _integration_emit_account_status() RETURNS trigger AS $$
BEGIN
    IF NEW.acc_status IS DISTINCT FROM OLD.acc_status THEN
        BEGIN
            IF EXISTS (SELECT 1 FROM webhook_endpoints
                       WHERE owner_id = NEW.owner_id AND is_active) THEN
                INSERT INTO integration_events (owner_id, event, payload)
                VALUES (NEW.owner_id,
                    CASE WHEN NEW.acc_status IN ('banned', 'spamblock', 'deactivated',
                                                 'session_expired', 'frozen')
                         THEN 'account.lost' ELSE 'account.status_changed' END,
                    jsonb_build_object(
                        'account_id', NEW.id,
                        'phone', NEW.phone,
                        'username', NEW.username,
                        'old_status', OLD.acc_status,
                        'new_status', NEW.acc_status));
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'integration event (account %) skipped: %', NEW.id, SQLERRM;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_account_status ON tg_accounts;
CREATE TRIGGER trg_integration_emit_account_status
    AFTER UPDATE OF acc_status ON tg_accounts
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_account_status();

-- Ограничение от Telegram: account.restricted.
CREATE OR REPLACE FUNCTION _integration_emit_restriction() RETURNS trigger AS $$
BEGIN
    BEGIN
        IF EXISTS (SELECT 1 FROM webhook_endpoints
                   WHERE owner_id = NEW.owner_id AND is_active) THEN
            INSERT INTO integration_events (owner_id, event, payload)
            VALUES (NEW.owner_id, 'account.restricted', jsonb_build_object(
                'account_id', NEW.account_id,
                'bot_id', NEW.bot_id,
                'kind', NEW.event_type,
                'severity', NEW.severity));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'integration event (restriction %) skipped: %', NEW.id, SQLERRM;
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_emit_restriction ON restriction_events;
CREATE TRIGGER trg_integration_emit_restriction
    AFTER INSERT ON restriction_events
    FOR EACH ROW
    EXECUTE FUNCTION _integration_emit_restriction();
