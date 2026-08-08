-- Account monitor: track last low-trust alert time to avoid spam
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS last_low_trust_alert TIMESTAMPTZ;
