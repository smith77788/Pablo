-- v33: Add missing columns and tables referenced in code but absent from schema

-- Add cluster column to tg_accounts (used by mass_ops.py for account filtering by cluster)
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS cluster TEXT;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_cluster
    ON tg_accounts(owner_id, cluster)
    WHERE cluster IS NOT NULL;

-- Create clusters table (used by channel_factory.py to assign channels to named clusters)
CREATE TABLE IF NOT EXISTS clusters (
    id         SERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, name)
);

CREATE INDEX IF NOT EXISTS idx_clusters_owner ON clusters(owner_id);
