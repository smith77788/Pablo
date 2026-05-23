-- Schema v8: bot notes
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS note TEXT;
