-- Менеджер по продажам: способы доставки и промокоды.
-- Раньше доставка/скидки были только свободным текстом order_rules — теперь
-- структурно: менеджер называет реальные способы/цены/сроки и применяет
-- валидные промокоды к сумме заказа (детерминированно, не на волю модели).

CREATE TABLE IF NOT EXISTS bot_sales_delivery (
    id          BIGSERIAL PRIMARY KEY,
    persona_id  BIGINT NOT NULL,
    owner_id    BIGINT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',      -- «Курьер», «Самовывоз», «Почта»
    price_cents INT  NOT NULL DEFAULT 0,        -- 0 = бесплатно
    eta         TEXT NOT NULL DEFAULT '',       -- «1-2 дня»
    zones       TEXT NOT NULL DEFAULT '',       -- «Москва, МО»
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_sales_delivery_persona ON bot_sales_delivery(persona_id);

CREATE TABLE IF NOT EXISTS bot_sales_promos (
    id              BIGSERIAL PRIMARY KEY,
    persona_id      BIGINT NOT NULL,
    owner_id        BIGINT NOT NULL,
    code            TEXT NOT NULL DEFAULT '',   -- «SALE10» (сравнение без регистра)
    percent         INT  NOT NULL DEFAULT 0,    -- размер скидки, %
    min_total_cents INT  NOT NULL DEFAULT 0,    -- минимальная сумма для применения
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at      TIMESTAMPTZ,                -- NULL = бессрочно
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_sales_promos_persona ON bot_sales_promos(persona_id);

-- Скидка/промокод в заказе.
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS discount_cents INT NOT NULL DEFAULT 0;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS promo_code TEXT NOT NULL DEFAULT '';
