-- Virtual Channel Administrator — персистентная модель канала (Channel Brain).
--
-- Хранит РЕДАКЦИОННУЮ ПОЛИТИКУ канала (не тексты постов): правила бренда,
-- рубрики (pillars) и их целевые доли, режим автономности, пороги гейта.
-- Её читает слой контроля качества (services/channel_brain.py) перед публикацией.
-- Только ADD/CREATE, идемпотентно (см. governance: миграции не переигрываются).

CREATE TABLE IF NOT EXISTS va_channel_brain (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT      NOT NULL,
    channel_key    TEXT        NOT NULL,   -- @username, -100…id или внутренний ключ
    brand_rules    JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- BrandRules (лимиты/слова)
    pillars        JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- список рубрик
    mix_weights    JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- целевые доли рубрик
    autonomy_mode  TEXT        NOT NULL DEFAULT 'manual'
                   CHECK (autonomy_mode IN ('manual','semi','autonomous')),
    max_streak     INTEGER     NOT NULL DEFAULT 2,   -- не более N одинаковых рубрик подряд
    dup_threshold  REAL        NOT NULL DEFAULT 0.6, -- порог «почти-повтор» (0..1)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, channel_key)
);

CREATE INDEX IF NOT EXISTS idx_va_channel_brain_owner ON va_channel_brain(owner_id);
