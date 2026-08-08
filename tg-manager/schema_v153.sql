-- v153: Per-account CF Relay URL for unique IP per account
-- Each account can have its own Cloudflare Worker URL, giving it a unique edge IP.
-- Telegram sees Cloudflare edge IP instead of Railway/datacenter IP.

ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS cf_relay_url TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_tg_accounts_cf_relay ON tg_accounts(cf_relay_url) WHERE cf_relay_url IS NOT NULL;

-- Table for managing CF Worker deployments
CREATE TABLE IF NOT EXISTS cf_worker_pool (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    worker_url TEXT NOT NULL,
    region TEXT DEFAULT 'auto',
    status TEXT DEFAULT 'active',
    assigned_accounts INTEGER DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cf_worker_pool_owner ON cf_worker_pool(owner_id);
