-- schema_v76: Operation queue template support + OAuth email accounts

ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS template_id INT;

ALTER TABLE strike_email_accounts
    ADD COLUMN IF NOT EXISTS auth_type TEXT NOT NULL DEFAULT 'password',
    ADD COLUMN IF NOT EXISTS oauth_provider TEXT,
    ADD COLUMN IF NOT EXISTS oauth_refresh_token TEXT,
    ADD COLUMN IF NOT EXISTS oauth_access_token TEXT,
    ADD COLUMN IF NOT EXISTS oauth_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS oauth_scopes TEXT[] DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_strike_email_oauth
    ON strike_email_accounts(owner_id, oauth_provider)
    WHERE auth_type = 'oauth';
