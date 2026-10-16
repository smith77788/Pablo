-- Provider Marketplace — блок 1: провайдеры и каталог услуг.
-- Внешние компании регистрируются как провайдеры, проходят верификацию и
-- публикуют услуги в каталог. Заказы/биллинг/резеллеры — следующие блоки
-- (ссылаются на эти таблицы по FK).
--
-- Существующие host_offerings (P2P-аренда) и smm_panels (внешний SMM) НЕ трогаем:
-- это узкие частные случаи; mp_* — общий слой провайдерского маркетплейса.
-- Деньги — в МИНОРНЫХ ЕДИНИЦАХ (центах), BIGINT, без float.

CREATE TABLE IF NOT EXISTS mp_providers (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,            -- Telegram-пользователь, управляющий провайдером
    slug          TEXT UNIQUE NOT NULL,       -- стабильный публичный идентификатор
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT 'other',
    website       TEXT NOT NULL DEFAULT '',
    contact       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending|verified|rejected|suspended
    verified_at   TIMESTAMPTZ,
    verified_by   BIGINT,
    reject_reason TEXT NOT NULL DEFAULT '',
    -- репутация/здоровье (наполняется блоком заказов)
    rating        NUMERIC(3,2) NOT NULL DEFAULT 0,   -- 0..5
    success_rate  NUMERIC(5,2) NOT NULL DEFAULT 0,   -- 0..100
    total_orders  INT NOT NULL DEFAULT 0,
    -- интеграция
    api_base_url  TEXT NOT NULL DEFAULT '',
    webhook_url   TEXT NOT NULL DEFAULT '',
    webhook_secret_enc TEXT NOT NULL DEFAULT '',      -- token_vault (AES-256-GCM)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mp_providers_owner_idx    ON mp_providers(owner_id);
CREATE INDEX IF NOT EXISTS mp_providers_status_idx   ON mp_providers(status);
CREATE INDEX IF NOT EXISTS mp_providers_category_idx ON mp_providers(category);

-- API-ключи, которые Infragram ВЫДАЁТ провайдеру для доступа к Provider API.
-- Храним ТОЛЬКО хэш (sha256) — сырой ключ показывается провайдеру один раз.
CREATE TABLE IF NOT EXISTS mp_provider_api_keys (
    id           BIGSERIAL PRIMARY KEY,
    provider_id  BIGINT NOT NULL REFERENCES mp_providers(id) ON DELETE CASCADE,
    key_prefix   TEXT NOT NULL,             -- первые символы (для отображения/логов)
    key_hash     TEXT NOT NULL UNIQUE,      -- sha256(full key)
    scopes       TEXT NOT NULL DEFAULT 'catalog,orders',
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS mp_api_keys_provider_idx ON mp_provider_api_keys(provider_id);

-- Каталог: услуги провайдера.
CREATE TABLE IF NOT EXISTS mp_services (
    id            BIGSERIAL PRIMARY KEY,
    provider_id   BIGINT NOT NULL REFERENCES mp_providers(id) ON DELETE CASCADE,
    external_id   TEXT NOT NULL DEFAULT '',  -- id на стороне провайдера (для sync)
    category      TEXT NOT NULL DEFAULT 'other',
    resource_kind TEXT NOT NULL DEFAULT '',  -- какую потребность закрывает (proxy/accounts/...) — для рекомендаций
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    price_cents   BIGINT NOT NULL DEFAULT 0, -- цена за 1 unit, минорные единицы
    currency      TEXT NOT NULL DEFAULT 'USD',
    unit          TEXT NOT NULL DEFAULT 'unit',
    min_qty       INT NOT NULL DEFAULT 1,
    max_qty       INT NOT NULL DEFAULT 1000000,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    stock         INT NOT NULL DEFAULT -1,    -- -1 = без лимита
    sla_hours     INT NOT NULL DEFAULT 24,
    params_schema JSONB NOT NULL DEFAULT '[]'::jsonb,  -- поля формы заказа
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mp_services_provider_idx ON mp_services(provider_id);
CREATE INDEX IF NOT EXISTS mp_services_category_idx ON mp_services(category);
CREATE INDEX IF NOT EXISTS mp_services_kind_idx     ON mp_services(resource_kind);
CREATE INDEX IF NOT EXISTS mp_services_active_idx   ON mp_services(is_active);
-- external_id уникален в рамках провайдера, но только когда задан (для API-sync).
CREATE UNIQUE INDEX IF NOT EXISTS mp_services_provider_ext_uniq
    ON mp_services(provider_id, external_id) WHERE external_id <> '';

-- Аудит действий маркетплейса (провайдеры/каталог/заказы/выплаты — общий журнал).
CREATE TABLE IF NOT EXISTS mp_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    entity_type  TEXT NOT NULL,             -- provider|service|api_key|order|...
    entity_id    BIGINT,
    action       TEXT NOT NULL,             -- register|verify|reject|suspend|publish|...
    actor_id     BIGINT,                    -- кто сделал (user_id/admin); NULL = система
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mp_audit_entity_idx ON mp_audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS mp_audit_created_idx ON mp_audit_log(created_at DESC);
