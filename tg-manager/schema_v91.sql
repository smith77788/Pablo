-- schema_v91: reg_check_cache + managed_channels influence columns

-- Cache for entity registration/creation date lookups
CREATE TABLE IF NOT EXISTS reg_check_cache (
    entity_id   BIGINT  NOT NULL,
    entity_type TEXT    NOT NULL,
    entity_name TEXT,
    username    TEXT,
    reg_date    TIMESTAMPTZ,
    method      TEXT    NOT NULL DEFAULT 'id_interpolation',
    checked_by  BIGINT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_id, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_reg_check_cache_checked_at
    ON reg_check_cache(checked_at DESC);

-- Topology influence scoring columns
ALTER TABLE managed_channels
    ADD COLUMN IF NOT EXISTS members_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS avg_views     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS influence     NUMERIC(8,2) NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_managed_channels_influence
    ON managed_channels(owner_id, influence DESC);
