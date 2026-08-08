-- schema_v40.sql: Account Status Tracking

ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS acc_status       TEXT DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS status_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS status_reason    TEXT;

-- Valid statuses: active, cooldown, spamblock, banned, deactivated, session_expired, archived

CREATE INDEX IF NOT EXISTS idx_tg_accounts_status ON tg_accounts(owner_id, acc_status);
