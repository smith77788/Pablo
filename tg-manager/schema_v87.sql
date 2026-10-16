-- schema_v87: A/B experiment tracking improvements
-- Adds started_at/ended_at to experiments for sweep filtering,
-- and converted_at to experiment_assignments for precise conversion timing.

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS started_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ended_at    TIMESTAMPTZ;

-- Backfill: treat existing active experiments as started from created_at
UPDATE experiments
SET started_at = created_at
WHERE started_at IS NULL AND status IN ('active', 'completed', 'paused');

ALTER TABLE experiment_assignments
    ADD COLUMN IF NOT EXISTS converted_at TIMESTAMPTZ;

-- Index to speed up nightly winner sweep (active experiments only)
CREATE INDEX IF NOT EXISTS idx_experiments_active_sweep
    ON experiments(status, started_at)
    WHERE status = 'active';

-- Index to speed up conversion lookups
CREATE INDEX IF NOT EXISTS idx_exp_assignments_variant_converted
    ON experiment_assignments(variant_id, converted);

-- operation_queue: notified_at for recovery_engine dedup
-- (recovery_engine.py line 474 writes this; missing column silently skipped until now)
ALTER TABLE operation_queue
    ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_operation_queue_notified
    ON operation_queue(notified_at)
    WHERE notified_at IS NOT NULL;
