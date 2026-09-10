-- Менеджер по продажам: база знаний (FAQ) и эталонные примеры диалогов (few-shot).
-- FAQ — точные ответы на частые вопросы (сроки/гарантия/оплата/доставка), которые
-- менеджер выдаёт по смыслу дословно и с приоритетом над генерацией → меньше
-- «тупит»/выдумывает. Примеры — образцы «как надо отвечать», поднимают человечность
-- и попадание в тон.

CREATE TABLE IF NOT EXISTS bot_sales_faq (
    id          BIGSERIAL PRIMARY KEY,
    persona_id  BIGINT NOT NULL,
    owner_id    BIGINT NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL DEFAULT '',
    keywords    TEXT NOT NULL DEFAULT '',   -- доп. слова-триггеры, через запятую
    priority    INT  NOT NULL DEFAULT 0,     -- выше — важнее при конфликте
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_sales_faq_persona ON bot_sales_faq(persona_id);

CREATE TABLE IF NOT EXISTS bot_sales_examples (
    id            BIGSERIAL PRIMARY KEY,
    persona_id    BIGINT NOT NULL,
    owner_id      BIGINT NOT NULL,
    user_msg      TEXT NOT NULL DEFAULT '',
    assistant_msg TEXT NOT NULL DEFAULT '',
    ord           INT  NOT NULL DEFAULT 0,   -- порядок примеров
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_sales_examples_persona ON bot_sales_examples(persona_id);
