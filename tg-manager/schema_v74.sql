-- v74: indexes for db_maintenance pruning queries
-- Without these, DELETE on large tables does sequential scans

-- operation_log: prune by created_at (30-day retention)
ALTER TABLE operation_log ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
CREATE INDEX IF NOT EXISTS idx_op_log_created ON operation_log(created_at);

-- restriction_events: index already exists on created_at from schema_v27
-- but add partial index for the common "recent critical" query pattern
CREATE INDEX IF NOT EXISTS idx_restriction_events_owner_recent
    ON restriction_events(owner_id, created_at DESC)
    WHERE severity IN ('critical', 'warning');

-- account_flood_log: prune by created_at (30-day retention)
CREATE INDEX IF NOT EXISTS idx_flood_log_created ON account_flood_log(created_at);

-- search_snapshots: prune by captured_at (table's actual timestamp column, see schema_v18.sql)
CREATE INDEX IF NOT EXISTS idx_search_snapshots_checked ON search_snapshots(captured_at);

-- behavioral_events: compound index for the maintenance DELETE
-- (already has idx on occurred_at DESC, this adds composite for owner queries)
CREATE INDEX IF NOT EXISTS idx_behav_events_occurred ON behavioral_events(occurred_at);
