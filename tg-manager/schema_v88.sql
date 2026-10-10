-- schema_v88: Add "join" trigger type to funnels + performance index

-- Drop the CHECK constraint that only allows 'start' and 'keyword'
-- and replace it with one that also allows 'join'.
ALTER TABLE funnels
    DROP CONSTRAINT IF EXISTS funnels_trigger_type_check;

ALTER TABLE funnels
    ADD CONSTRAINT funnels_trigger_type_check
    CHECK (trigger_type IN ('start', 'keyword', 'join'));

-- Index to speed up the funnel runner's due-step query
-- (filters on completed=false AND next_send_at <= now())
CREATE INDEX IF NOT EXISTS idx_funnel_subs_due
    ON funnel_subscriptions (next_send_at)
    WHERE completed = false;
