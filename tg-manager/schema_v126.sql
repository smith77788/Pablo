-- v126: Add label column to operation_queue (used by mini app API and bot handlers)
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS label TEXT;
