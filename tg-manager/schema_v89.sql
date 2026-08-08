-- schema_v89: Funnel stat counters + dropped tracking

-- Add entered/completed/dropped counters to funnels table
ALTER TABLE funnels
    ADD COLUMN IF NOT EXISTS entered_count   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS completed_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dropped_count   INTEGER NOT NULL DEFAULT 0;

-- Backfill entered_count from existing subscriptions
UPDATE funnels f
   SET entered_count = (
       SELECT COUNT(*) FROM funnel_subscriptions fs WHERE fs.funnel_id = f.id
   )
   WHERE entered_count = 0;

-- Backfill completed_count from existing completed subscriptions
UPDATE funnels f
   SET completed_count = (
       SELECT COUNT(*) FROM funnel_subscriptions fs
       WHERE fs.funnel_id = f.id AND fs.completed = true
   )
   WHERE completed_count = 0;

-- Add dropped column to funnel_subscriptions
ALTER TABLE funnel_subscriptions
    ADD COLUMN IF NOT EXISTS dropped BOOLEAN NOT NULL DEFAULT false;

-- Index to speed up dropped queries
CREATE INDEX IF NOT EXISTS idx_funnel_subs_dropped
    ON funnel_subscriptions (funnel_id, dropped)
    WHERE dropped = true;

-- Active subscriptions index (excludes both completed and dropped)
CREATE INDEX IF NOT EXISTS idx_funnel_subs_active
    ON funnel_subscriptions (next_send_at)
    WHERE completed = false AND dropped = false;
