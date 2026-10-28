-- schema_v34: per-account proxy assignment
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS proxy_id INT REFERENCES user_proxies(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_proxy ON tg_accounts(proxy_id) WHERE proxy_id IS NOT NULL;
