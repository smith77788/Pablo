-- v21: Add access_hash to managed_channels for direct Telethon entity resolution
ALTER TABLE managed_channels ADD COLUMN IF NOT EXISTS access_hash BIGINT NOT NULL DEFAULT 0;
