-- v215: связывание устройства — автономный вход вне Telegram (PWA/Android).
--
-- Мини-апп входит только через Telegram initData. Отдельному приложению
-- (PWA, потом APK) initData взять неоткуда, поэтому нужен свой вход, но с той
-- же точкой доверия — Telegram. Схема: бот выдаёт одноразовый КОД связывания;
-- приложение меняет код на долгоживущий ТОКЕН УСТРОЙСТВА; дальше приложение
-- меняет токен устройства на обычный короткий сессионный токен (тот же, что и
-- Telegram-вход) — так остальные ~500 маршрутов не меняются.
--
-- Сам токен устройства НЕ храним (только его отпечаток sha256) — как и сессии
-- в token_vault: утечка таблицы не отдаёт действующий доступ. Отпечаток нужен
-- для отзыва конкретного устройства.

CREATE TABLE IF NOT EXISTS device_pairings (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    -- Одноразовый код связывания. NULL после погашения (обменян на токен).
    code           TEXT,
    code_expires_at TIMESTAMPTZ,
    -- Отпечаток выданного токена устройства (sha256 hex). Сам токен не хранится.
    token_fp       TEXT,
    device_label   TEXT DEFAULT '',            -- «Android · Chrome» и т.п.
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    paired_at      TIMESTAMPTZ,                -- когда код обменяли на токен
    last_seen_at   TIMESTAMPTZ,               -- последний обмен токена на сессию
    revoked_at     TIMESTAMPTZ                -- отзыв устройства владельцем
);

-- Быстрый поиск активного кода при обмене.
CREATE INDEX IF NOT EXISTS idx_device_pairings_code
    ON device_pairings(code) WHERE code IS NOT NULL;
-- Список/отзыв устройств владельца и проверка отпечатка при обмене токена.
CREATE INDEX IF NOT EXISTS idx_device_pairings_owner
    ON device_pairings(owner_id);
CREATE INDEX IF NOT EXISTS idx_device_pairings_fp
    ON device_pairings(token_fp) WHERE token_fp IS NOT NULL;
