-- schema_v92: extend reg_check_cache with rich metadata columns
ALTER TABLE reg_check_cache
    ADD COLUMN IF NOT EXISTS participants_count INTEGER,
    ADD COLUMN IF NOT EXISTS verified           BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS scam              BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS fake              BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS premium           BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS about             TEXT,
    ADD COLUMN IF NOT EXISTS confidence_lo     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS confidence_hi     TIMESTAMPTZ;
