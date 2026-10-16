-- schema_v55: Strike msgs_fetched diagnostic field
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS msgs_fetched INT DEFAULT 0;
