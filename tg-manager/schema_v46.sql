-- v46: Drift detection — track about text and last check timestamp in managed_channels
ALTER TABLE managed_channels
    ADD COLUMN IF NOT EXISTS about            TEXT,
    ADD COLUMN IF NOT EXISTS last_drift_check TIMESTAMPTZ;
