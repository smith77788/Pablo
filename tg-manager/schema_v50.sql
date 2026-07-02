-- v50: Add params column to dm_campaigns (used for cohort_type and other campaign parameters)
ALTER TABLE dm_campaigns
    ADD COLUMN IF NOT EXISTS params JSONB DEFAULT '{}';
