-- schema_v42: DM-кампании + retry intelligence в operation_queue
-- DM Campaign tables
CREATE TABLE IF NOT EXISTS dm_campaigns (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    name           TEXT NOT NULL,
    text_template  TEXT NOT NULL,
    target_type    TEXT NOT NULL DEFAULT 'bot_users',  -- 'bot_users'|'crm'
    target_id      BIGINT,           -- bot_id для bot_users, NULL для crm
    status         TEXT DEFAULT 'draft',  -- draft/running/paused/done/failed
    sent_count     INT DEFAULT 0,
    fail_count     INT DEFAULT 0,
    total_targets  INT DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT now(),
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_dm_campaigns_owner ON dm_campaigns(owner_id, status);

CREATE TABLE IF NOT EXISTS dm_campaign_log (
    id          BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL REFERENCES dm_campaigns(id) ON DELETE CASCADE,
    account_id  INT,
    tg_user_id  BIGINT NOT NULL,
    status      TEXT DEFAULT 'sent',  -- sent/failed/blocked/skip
    error_msg   TEXT,
    sent_at     TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dm_log_dedup
    ON dm_campaign_log(campaign_id, tg_user_id)
    WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS idx_dm_log_campaign ON dm_campaign_log(campaign_id);

-- Retry intelligence fields for operation_queue
ALTER TABLE operation_queue
    ADD COLUMN IF NOT EXISTS retry_count  INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_retries  INT DEFAULT 3,
    ADD COLUMN IF NOT EXISTS last_error   TEXT;
