-- Каналы-образцы виртуального администратора: конкуренты или свой успешный
-- канал. Администратор читает их публичные посты, считает цифры (stats) и
-- разбирает подачу (lessons), чтобы писать в том же качестве, не копируя.
CREATE TABLE IF NOT EXISTS va_reference_channels (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT      NOT NULL,
    channel_id    BIGINT      NOT NULL,
    ref_username  TEXT        NOT NULL,
    kind          TEXT        NOT NULL DEFAULT 'competitor'
                  CHECK (kind IN ('competitor','own','example')),
    status        TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','ready','error')),
    error         TEXT,
    stats         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    lessons       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    attempted_at  TIMESTAMPTZ,
    analyzed_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_va_reference_channels
    ON va_reference_channels(owner_id, channel_id, lower(ref_username));
CREATE INDEX IF NOT EXISTS idx_va_reference_channels_due
    ON va_reference_channels(attempted_at NULLS FIRST);
