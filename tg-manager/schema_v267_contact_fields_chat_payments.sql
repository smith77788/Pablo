-- schema_v267: поля контакта для сценариев и оплата заказа прямо в чате.
--
-- bot_users.fields — произвольные поля контакта, которые сценарий собирает
-- вопросами («как вас зовут», «телефон», «город»). Подставляются в тексты
-- как {{имя_поля}} и проверяются условиями сценария.
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS fields JSONB NOT NULL DEFAULT '{}';

-- Оплата заказа ИИ-продавца прямо в чате (Telegram Payments / Звёзды).
-- payment_in_chat: '' — выключено, 'stars' — Звёзды, 'provider' — карта
-- через платёжного провайдера, токен провайдера хранится зашифрованным
-- (services/token_vault.py) и наружу не отдаётся.
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS payment_in_chat TEXT NOT NULL DEFAULT '';
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS stars_per_unit INT NOT NULL DEFAULT 0;
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS payment_provider_token TEXT;

ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS invoice_sent_at   TIMESTAMPTZ;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS invoice_currency  TEXT;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS invoice_amount    BIGINT;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS paid_at           TIMESTAMPTZ;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS paid_amount       BIGINT;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS paid_currency     TEXT;
ALTER TABLE bot_sales_orders ADD COLUMN IF NOT EXISTS payment_charge_id TEXT;
-- Один платёж Telegram — одна оплата: повтор апдейта не задваивает выручку.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_sales_orders_charge
    ON bot_sales_orders (payment_charge_id) WHERE payment_charge_id IS NOT NULL AND payment_charge_id <> '';
