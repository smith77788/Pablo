-- v17: Add notify_enabled column to tracked_keywords
ALTER TABLE tracked_keywords ADD COLUMN IF NOT EXISTS notify_enabled BOOLEAN NOT NULL DEFAULT TRUE;
