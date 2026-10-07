-- Одобренные владельцем фотографии отдельно для каждого канала.
CREATE TABLE IF NOT EXISTS va_media (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL,
    description TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    selected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, channel_id, file_unique_id)
);
CREATE INDEX IF NOT EXISTS idx_va_media_channel
    ON va_media(owner_id, channel_id, selected_at) WHERE enabled;
ALTER TABLE va_admin_drafts ADD COLUMN IF NOT EXISTS media_id BIGINT REFERENCES va_media(id);
