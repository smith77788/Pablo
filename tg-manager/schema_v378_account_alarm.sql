-- schema_v378: сигналы атаки на аккаунт флота (services/account_alarm.py).
--
-- Дозор чужих сеансов видит угон, когда вход уже случился. Раньше него видны:
-- чужой запрос кода входа (служебный чат 777000), запрос сброса пароля 2FA,
-- смена или снятие пароля не через Infragram, новая почта восстановления.
--
-- service_seen_msg_id — последнее разобранное сообщение чата 777000: тревога
--   только о новом.
-- login_code_read_at — владелец прочитал код кнопкой «Код входа»: этот код его.
-- twofa_reset_until — когда сработает запрошенный сброс пароля 2FA (NULL — нет).
-- twofa_mismatch_at — сохранённый пароль 2FA перестал подходить.
-- recovery_email_fp — отпечаток почты восстановления: тревога о смене.
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS service_seen_msg_id BIGINT;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS login_code_read_at TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS twofa_reset_until TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS twofa_mismatch_at TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS recovery_email_fp TEXT;

-- Журнал безопасности: что нашли и что сделали (карточка аккаунта, «Защита»).
CREATE TABLE IF NOT EXISTS account_security_events (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    account_id  BIGINT,
    kind        TEXT        NOT NULL,
    detail      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_security_events_owner
    ON account_security_events (owner_id, created_at DESC);

-- Автозащита пароля по умолчанию у всех (решение владельца продукта 21.11.2026):
-- ставить свой пароль 2FA аккаунтам без него и отменять чужой сброс пароля.
-- Закрытие чужих сеансов остаётся выбором каждого владельца: оно может
-- закрыть его собственный телефон.
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS guard_reset BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE shield_configs ALTER COLUMN guard_twofa SET DEFAULT TRUE;
UPDATE shield_configs SET guard_twofa = TRUE WHERE guard_twofa = FALSE;
