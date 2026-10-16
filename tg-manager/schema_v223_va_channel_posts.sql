-- Virtual Channel Administrator — история опубликованных постов на канал (Content Memory).
--
-- ЗАЧЕМ. Антиповтор редактора (services/channel_brain.repetition_check) сравнивает
-- черновик с НЕДАВНИМИ постами КОНКРЕТНОГО канала. Такой истории раньше негде было
-- взять: operation_log хранит ФАКТ публикации (op/шаг/цель/статус), но не ТЕЛО поста
-- на канал. Эта таблица — источник recent_texts: тело каждого реально
-- опубликованного поста, owner-scoped, чтобы гейт качества работал на настоящей
-- истории, а не на пустышке.
-- Только ADD/CREATE, идемпотентно (governance: миграции не переигрываются).

CREATE TABLE IF NOT EXISTS va_channel_posts (
    id            BIGSERIAL   PRIMARY KEY,
    owner_id      BIGINT      NOT NULL,
    channel_key   TEXT        NOT NULL,   -- str(channel_id) / @username / внутренний ключ
    op_id         BIGINT,                 -- операция публикации (трассировка), может быть NULL
    pillar        TEXT,                   -- рубрика поста для контент-микса, может быть NULL
    body          TEXT        NOT NULL DEFAULT '',  -- опубликованный текст (для антиповтора)
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Чтение гейта: последние N постов канала владельца, свежие сверху.
CREATE INDEX IF NOT EXISTS idx_va_channel_posts_recent
    ON va_channel_posts(owner_id, channel_key, published_at DESC);
