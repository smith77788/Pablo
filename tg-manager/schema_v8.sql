-- Schema v8: bot notes
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS note TEXT;

-- Add is_blocked column to bot_users
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT FALSE;
