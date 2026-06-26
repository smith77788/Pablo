-- schema_v80: Account Infrastructure columns (tags, pool, labels, warnings, project)
-- Эти колонки используются в resource_selector, flood_engine, db.py, op_helpers
-- и предполагались существующими с v60, но миграция не была добавлена.
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS tags     TEXT[] DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS pool     TEXT   DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS labels   TEXT[] DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS warnings TEXT[] DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS project  TEXT   DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_pool
    ON tg_accounts(owner_id, pool)
    WHERE pool IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_tags
    ON tg_accounts USING GIN(tags)
    WHERE array_length(tags, 1) > 0;
