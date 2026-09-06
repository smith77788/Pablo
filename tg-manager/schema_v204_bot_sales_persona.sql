-- Сущность бота: живой менеджер по продажам.
-- Провайдер-каталог маркетплейса (mp_*) — про внешних поставщиков; здесь другое:
-- персона, которую владелец назначает своему боту (managed_bots), чтобы бот вёл
-- себя как реальный менеджер: консультирует, называет цены, принимает заказы,
-- переводит на живого оператора (reuse relay_sessions), даёт ссылки на каналы,
-- поддерживает small-talk и помнит предпочтения клиента.
-- Деньги — в минорных единицах (центах), BIGINT.

CREATE TABLE IF NOT EXISTS bot_sales_personas (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    bot_id         BIGINT,                       -- назначенный бот (managed_bots.bot_id); NULL = черновик
    -- личность
    name           TEXT NOT NULL,
    role_title     TEXT NOT NULL DEFAULT 'менеджер по продажам',
    gender         TEXT NOT NULL DEFAULT 'unspecified',  -- male|female|unspecified
    age            INT,
    avatar_emoji   TEXT NOT NULL DEFAULT '🧑‍💼',
    personality    TEXT NOT NULL DEFAULT '',     -- свободный текст: манера, характер
    tone           TEXT NOT NULL DEFAULT 'friendly',     -- friendly|professional|casual|warm|energetic
    formality      TEXT NOT NULL DEFAULT 'auto', -- ty|vy|auto (auto = подстроиться под клиента)
    humor_level    INT NOT NULL DEFAULT 1,       -- 0..3
    emoji_level    TEXT NOT NULL DEFAULT 'medium',-- none|low|medium|high
    msg_length     TEXT NOT NULL DEFAULT 'medium',-- short|medium|long
    language       TEXT NOT NULL DEFAULT 'ru',
    mirror_language BOOLEAN NOT NULL DEFAULT TRUE,-- отвечать на языке клиента
    -- что продаёт / компания
    company_name   TEXT NOT NULL DEFAULT '',
    company_about  TEXT NOT NULL DEFAULT '',
    product_knowledge TEXT NOT NULL DEFAULT '',   -- общее знание об ассортименте (свободный текст)
    pricing_policy TEXT NOT NULL DEFAULT '',      -- политика цен/скидок (текст)
    disclose_prices BOOLEAN NOT NULL DEFAULT TRUE,
    currency       TEXT NOT NULL DEFAULT 'USD',
    -- поведение (тумблеры)
    can_take_orders     BOOLEAN NOT NULL DEFAULT TRUE,
    can_consult         BOOLEAN NOT NULL DEFAULT TRUE,
    can_smalltalk       BOOLEAN NOT NULL DEFAULT TRUE,
    can_discuss_prefs   BOOLEAN NOT NULL DEFAULT TRUE,
    proactive_offers    BOOLEAN NOT NULL DEFAULT TRUE,
    smalltalk_topics    TEXT NOT NULL DEFAULT '', -- разрешённые темы для беседы
    taboo_topics        TEXT NOT NULL DEFAULT '', -- о чём никогда не говорить
    -- оператор / каналы / заказы
    operator_username   TEXT NOT NULL DEFAULT '', -- @оператор (для перевода на живого человека)
    operator_chat_id    BIGINT,                   -- либо явный chat_id оператора
    handoff_triggers    TEXT NOT NULL DEFAULT '', -- ключевые слова → перевод на оператора
    handoff_message     TEXT NOT NULL DEFAULT 'Секунду, подключаю живого специалиста 🙌',
    channels            JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{title,url}] ссылки на каналы
    order_fields        JSONB NOT NULL DEFAULT '["Имя","Телефон","Адрес доставки"]'::jsonb,
    order_confirm_message TEXT NOT NULL DEFAULT 'Спасибо! Ваш заказ принят, скоро с вами свяжутся ✅',
    -- тексты
    greeting       TEXT NOT NULL DEFAULT '',
    fallback       TEXT NOT NULL DEFAULT 'Дайте секунду, уточню и вернусь с ответом 🙌',
    guardrails     TEXT NOT NULL DEFAULT '',      -- доп. запреты/правила честности
    -- модель
    ai_provider    TEXT NOT NULL DEFAULT '',      -- пусто = автo (первый настроенный)
    model          TEXT NOT NULL DEFAULT '',
    max_tokens     INT NOT NULL DEFAULT 400,
    temperature    NUMERIC(3,2) NOT NULL DEFAULT 0.7,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bsp_owner_idx ON bot_sales_personas(owner_id);
-- один активный персонаж на бота
CREATE UNIQUE INDEX IF NOT EXISTS bsp_bot_active_uniq
    ON bot_sales_personas(bot_id) WHERE bot_id IS NOT NULL AND is_active;

-- Товары, которые персона реально знает и продаёт (без галлюцинаций цен).
CREATE TABLE IF NOT EXISTS bot_sales_products (
    id           BIGSERIAL PRIMARY KEY,
    persona_id   BIGINT NOT NULL REFERENCES bot_sales_personas(id) ON DELETE CASCADE,
    owner_id     BIGINT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    sku          TEXT NOT NULL DEFAULT '',
    price_cents  BIGINT NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'USD',
    in_stock     BOOLEAN NOT NULL DEFAULT TRUE,
    attributes   JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bsp_prod_persona_idx ON bot_sales_products(persona_id);

-- Заказы, которые персона приняла в диалоге.
CREATE TABLE IF NOT EXISTS bot_sales_orders (
    id               BIGSERIAL PRIMARY KEY,
    owner_id         BIGINT NOT NULL,
    bot_id           BIGINT NOT NULL,
    persona_id       BIGINT REFERENCES bot_sales_personas(id) ON DELETE SET NULL,
    customer_chat_id BIGINT NOT NULL,
    customer_username TEXT NOT NULL DEFAULT '',
    customer_name    TEXT NOT NULL DEFAULT '',
    items            JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{name,qty,price_cents}]
    total_cents      BIGINT NOT NULL DEFAULT 0,
    currency         TEXT NOT NULL DEFAULT 'USD',
    status           TEXT NOT NULL DEFAULT 'new',  -- new|confirmed|handoff|cancelled|done
    contact          JSONB NOT NULL DEFAULT '{}'::jsonb,  -- собранные поля (телефон/адрес/…)
    note             TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bso_owner_idx ON bot_sales_orders(owner_id);
CREATE INDEX IF NOT EXISTS bso_bot_idx ON bot_sales_orders(bot_id);
CREATE INDEX IF NOT EXISTS bso_status_idx ON bot_sales_orders(status);

-- Память по каждому клиенту: стадия воронки, предпочтения, последние реплики.
CREATE TABLE IF NOT EXISTS bot_sales_dialogs (
    id               BIGSERIAL PRIMARY KEY,
    bot_id           BIGINT NOT NULL,
    customer_chat_id BIGINT NOT NULL,
    owner_id         BIGINT NOT NULL,
    persona_id       BIGINT REFERENCES bot_sales_personas(id) ON DELETE SET NULL,
    stage            TEXT NOT NULL DEFAULT 'greeting',  -- greeting|consult|offer|order|handoff|closed
    prefs            JSONB NOT NULL DEFAULT '{}'::jsonb, -- что узнали о клиенте
    history          JSONB NOT NULL DEFAULT '[]'::jsonb, -- последние ходы [{role,content}]
    summary          TEXT NOT NULL DEFAULT '',
    handed_off       BOOLEAN NOT NULL DEFAULT FALSE,
    msg_count        INT NOT NULL DEFAULT 0,
    last_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bot_id, customer_chat_id)
);
CREATE INDEX IF NOT EXISTS bsd_owner_idx ON bot_sales_dialogs(owner_id);
