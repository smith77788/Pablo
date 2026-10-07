-- Strike mode preference per user
ALTER TABLE strike_access ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'normal';
COMMENT ON COLUMN strike_access.mode IS 'fast | normal | maximum';
