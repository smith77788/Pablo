-- Менеджер по продажам: пакет «готового продукта» — часы работы, стиль продаж,
-- пороги заказа/доставки, фоллоуап, приветствие по времени, антифлуд, потолок
-- скидки, 18+, подтверждение оплаты; расширение товара (остатки, варианты, связки).
-- Всё аддитивно (ADD COLUMN IF NOT EXISTS), безопасные DEFAULT.

-- ── Часы работы / оффхаус ─────────────────────────────────────────────────────
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS work_start TEXT NOT NULL DEFAULT '';   -- "09:00"
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS work_end   TEXT NOT NULL DEFAULT '';   -- "21:00"
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS work_days  TEXT NOT NULL DEFAULT '';   -- "1-5" / "1-7" / "1,2,3"
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS tz_offset  INT  NOT NULL DEFAULT 0;     -- часы к UTC (Москва = 3)
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS offhours_message TEXT NOT NULL DEFAULT '';

-- ── Стиль продаж / рамки темы ─────────────────────────────────────────────────
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS sales_intensity TEXT NOT NULL DEFAULT 'balanced'; -- soft|balanced|aggressive
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS scope_guard BOOLEAN NOT NULL DEFAULT FALSE;       -- отвечать только по нашим товарам/темам
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS greeting_by_time BOOLEAN NOT NULL DEFAULT FALSE;  -- «доброе утро/день/вечер»

-- ── Пороги заказа/доставки ────────────────────────────────────────────────────
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS min_order_total INT NOT NULL DEFAULT 0;         -- центы, 0 = без порога
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS free_delivery_threshold INT NOT NULL DEFAULT 0; -- центы, 0 = выкл

-- ── Фоллоуап (дожим замолчавшего клиента) ─────────────────────────────────────
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS followup_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS followup_delay_min INT NOT NULL DEFAULT 60;
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS followup_message TEXT NOT NULL DEFAULT '';

-- ── Безопасность / антифлуд / скидки / 18+ / оплата ──────────────────────────
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS rate_limit_per_min INT NOT NULL DEFAULT 0;   -- 0 = выкл
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS discount_max_percent INT NOT NULL DEFAULT 0; -- потолок скидки без оператора
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS require_age_confirm BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS age_confirm_message TEXT NOT NULL DEFAULT '';
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS require_payment_proof BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS notify_channel_chat_id BIGINT;  -- доп. цель уведомлений (канал/чат)

-- Отметка последнего исходящего фоллоуапа по диалогу (антидубль дожима).
ALTER TABLE bot_sales_dialogs ADD COLUMN IF NOT EXISTS followup_sent_at TIMESTAMPTZ;
ALTER TABLE bot_sales_dialogs ADD COLUMN IF NOT EXISTS age_confirmed BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Расширение товара ─────────────────────────────────────────────────────────
ALTER TABLE bot_sales_products ADD COLUMN IF NOT EXISTS stock_qty INT;                        -- NULL = неограниченно
ALTER TABLE bot_sales_products ADD COLUMN IF NOT EXISTS related_skus TEXT NOT NULL DEFAULT ''; -- связки (кросс-сейл), через запятую
ALTER TABLE bot_sales_products ADD COLUMN IF NOT EXISTS variants JSONB NOT NULL DEFAULT '[]';  -- [{name, price_cents}]
