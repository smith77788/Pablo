-- schema_v94: OSINT enrichment columns for reg_check_cache
ALTER TABLE reg_check_cache
    ADD COLUMN IF NOT EXISTS dc_id             SMALLINT,
    ADD COLUMN IF NOT EXISTS is_fragment       BOOLEAN  DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS confidence_score  NUMERIC(3,2),
    ADD COLUMN IF NOT EXISTS oldest_photo_id   BIGINT,
    ADD COLUMN IF NOT EXISTS first_spotted_at  TIMESTAMPTZ;
