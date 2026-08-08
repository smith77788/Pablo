-- Schema v56: phone field for bot users + real health check support

-- Store phone number when user explicitly shares it (contact message)
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS phone TEXT;

-- Index for phone lookups
CREATE INDEX IF NOT EXISTS idx_bot_users_phone ON bot_users(phone) WHERE phone IS NOT NULL;

-- last_check_at: when we last ran a real Telegram check on this account
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS last_real_check_at TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS real_check_status TEXT; -- 'ok'|'spamblock'|'restricted'|'expired'|'error'
