-- schema_v68: EPOCH III — Global Presence → Ecosystem integration
-- Adds ecosystem_id to global_presence_plans so GP plans auto-create ecosystems

ALTER TABLE global_presence_plans
    ADD COLUMN IF NOT EXISTS ecosystem_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_gp_plans_ecosystem
    ON global_presence_plans(ecosystem_id)
    WHERE ecosystem_id IS NOT NULL;
