-- Schema v64: account warmup level tracking
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS last_warmup_at TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS warmup_level   TEXT        DEFAULT NULL;
