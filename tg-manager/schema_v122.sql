-- schema_v122: Growth Agent — добавить целевую сущность и стратегию

ALTER TABLE growth_goals
    ADD COLUMN IF NOT EXISTS target_entity_type VARCHAR(20) DEFAULT 'bot',
    ADD COLUMN IF NOT EXISTS target_entity_id   BIGINT,
    ADD COLUMN IF NOT EXISTS target_entity_label TEXT;
