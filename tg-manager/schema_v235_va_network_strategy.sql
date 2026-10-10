-- Shared strategy is opt-in; existing channel administrators keep their settings.
CREATE TABLE IF NOT EXISTS va_network_strategy (
    owner_id BIGINT PRIMARY KEY,
    settings JSONB NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(settings) = 'object')
);

ALTER TABLE va_reference_channels ADD COLUMN IF NOT EXISTS focus TEXT NOT NULL DEFAULT '';
