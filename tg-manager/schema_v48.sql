-- v48: Add type column to managed_channels for channel/group distinction
ALTER TABLE managed_channels ADD COLUMN IF NOT EXISTS type TEXT;
CREATE INDEX IF NOT EXISTS idx_managed_channels_type ON managed_channels(owner_id, type);
