-- schema_v73: behavioral_events composite index + periodic pruning support
-- The _recompute_scores query in behavioral_engine.py filters
--   WHERE occurred_at > now() - INTERVAL '30 days'
-- and groups by (owner_id, entity_type, entity_id). With only the existing
-- single-column idx_be_occurred index, at 6 months of active use (millions of
-- rows) the planner does a full seqscan every 15 minutes.
--
-- Add a composite index so the planner can satisfy both the time filter
-- and the owner_id GROUP BY with a single index scan.

DROP INDEX IF EXISTS idx_be_occurred;

CREATE INDEX IF NOT EXISTS idx_be_owner_occurred
    ON behavioral_events(owner_id, occurred_at DESC);
