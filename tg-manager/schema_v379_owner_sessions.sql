-- schema_v379: входы владельца в Infragram (services/owner_sessions.py).
--
-- Сессионный токен самоподписан и живёт два часа; кто вошёл, с какого
-- устройства и откуда, не записывалось нигде, и завершить чужой вход было
-- нечем. Теперь каждый вход — строка здесь, номер сеанса (sid) едет в токене,
-- а завершённый сеанс отбивается на каждом запросе.
--
-- device_fp — отпечаток строки браузера (без адреса: адрес телефона меняется
--   при каждом переходе Wi-Fi → мобильная сеть).
-- pairing_id — связанное устройство (device_pairings): завершение сеанса
--   отзывает и его ключ.
CREATE TABLE IF NOT EXISTS owner_sessions (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT      NOT NULL,
    sid           TEXT        NOT NULL UNIQUE,
    kind          TEXT        NOT NULL DEFAULT 'telegram',
    device_fp     TEXT        NOT NULL,
    device_label  TEXT        NOT NULL DEFAULT '',
    user_agent    TEXT        NOT NULL DEFAULT '',
    ip            TEXT        NOT NULL DEFAULT '',
    country       TEXT        NOT NULL DEFAULT '',
    pairing_id    BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_owner_sessions_owner
    ON owner_sessions (owner_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_owner_sessions_revoked
    ON owner_sessions (revoked_at) WHERE revoked_at IS NOT NULL;
