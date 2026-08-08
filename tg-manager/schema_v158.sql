-- schema_v158: модуль Host-Server — покупаемый доступ + маркетплейс инфраструктуры.
--
-- Доступ к модулю (разовая лицензия, как strike_access). Цена — не здесь, а в
-- platform_settings (key='host_server_price_usd'), чтобы админ менял её без деплоя.
CREATE TABLE IF NOT EXISTS host_server_access (
    user_id      BIGINT PRIMARY KEY,
    purchased_at TIMESTAMPTZ DEFAULT now(),
    payment_ref  TEXT,
    granted_by   BIGINT
);

-- Предложения аренды инфраструктуры (пользователь СДАЁТ своё).
--   kind: 'proxy'   — прокси/канал,
--         'server'  — выделенный сервер/VPS под инфраструктуру,
--         'device_compute' — вычислительные мощности своего устройства
--                            (с согласия: на нём ставится и работает инфра
--                            арендаторов). Согласие фиксируется allow_device_compute.
CREATE TABLE IF NOT EXISTS host_offerings (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    kind          TEXT   NOT NULL DEFAULT 'server',
    title         TEXT   NOT NULL,
    description   TEXT,
    specs         JSONB,
    region        TEXT,
    price_usd     NUMERIC(12,2) NOT NULL DEFAULT 0,
    period        TEXT   NOT NULL DEFAULT 'month',   -- hour|day|month
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    -- Явное согласие владельца на использование его устройства под чужую инфру.
    -- Для kind='device_compute' обязано быть TRUE, иначе предложение не публикуется.
    allow_device_compute BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_offerings_owner ON host_offerings(owner_id);
CREATE INDEX IF NOT EXISTS idx_host_offerings_active ON host_offerings(is_active, kind);

-- Аренды (пользователь БЕРЁТ чужое). provider_id денормализован для скоупинга
-- входящих аренд без джойна.
CREATE TABLE IF NOT EXISTS host_rentals (
    id            BIGSERIAL PRIMARY KEY,
    offering_id   BIGINT NOT NULL REFERENCES host_offerings(id) ON DELETE CASCADE,
    tenant_id     BIGINT NOT NULL,
    provider_id   BIGINT NOT NULL,
    status        TEXT   NOT NULL DEFAULT 'pending',  -- pending|active|ended|cancelled|rejected
    period        TEXT   NOT NULL DEFAULT 'month',
    units         INT    NOT NULL DEFAULT 1,          -- сколько периодов
    price_usd     NUMERIC(12,2) NOT NULL DEFAULT 0,   -- цена на момент аренды (снапшот)
    started_at    TIMESTAMPTZ,
    ends_at       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_rentals_tenant ON host_rentals(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_host_rentals_provider ON host_rentals(provider_id, status);
