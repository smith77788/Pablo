-- v85: add cooldown_cleared_at to tg_accounts
-- Tracks when cooldown_until was last cleared (expired naturally).
-- Used by trust_engine auto-rotate to give accounts a grace period before
-- re-applying cooldown, preventing perpetual re-cooling after flood events.
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS cooldown_cleared_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_cooldown_cleared
    ON tg_accounts(owner_id, cooldown_cleared_at)
    WHERE cooldown_cleared_at IS NOT NULL;
