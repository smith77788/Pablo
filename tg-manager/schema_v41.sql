-- v41: Infrastructure OS layer — flood intelligence, parser, proxy health, account warmup, audit

-- Enhanced flood tracking (more detail than existing account_flood_log)
ALTER TABLE account_flood_log
    ADD COLUMN IF NOT EXISTS action_type       TEXT DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS consecutive_count INT  DEFAULT 1,
    ADD COLUMN IF NOT EXISTS actual_wait       INT,
    ADD COLUMN IF NOT EXISTS operation_id      BIGINT;

-- Proxy health tracking
CREATE TABLE IF NOT EXISTS proxy_health_log (
    id             BIGSERIAL PRIMARY KEY,
    proxy_id       BIGINT NOT NULL,
    owner_id       BIGINT NOT NULL,
    checked_at     TIMESTAMPTZ DEFAULT now(),
    is_reachable   BOOLEAN NOT NULL,
    latency_ms     INT,
    dc_id          INT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_proxy_health_proxy ON proxy_health_log(proxy_id, checked_at DESC);

-- Proxy scoring (denormalized scores updated by health checker)
ALTER TABLE user_proxies
    ADD COLUMN IF NOT EXISTS geo_country      TEXT,
    ADD COLUMN IF NOT EXISTS geo_city         TEXT,
    ADD COLUMN IF NOT EXISTS latency_avg_ms   INT,
    ADD COLUMN IF NOT EXISTS success_rate     NUMERIC(5,2) DEFAULT 100,
    ADD COLUMN IF NOT EXISTS last_checked_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dc_affinity      INT,       -- preferred DC
    ADD COLUMN IF NOT EXISTS proxy_type       TEXT DEFAULT 'socks5';  -- socks5/http/mtproto

-- Account warmup plans
CREATE TABLE IF NOT EXISTS account_warmup_plans (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    account_id     BIGINT NOT NULL,
    plan_type      TEXT NOT NULL DEFAULT 'standard',  -- standard/aggressive/gentle
    current_day    INT  DEFAULT 0,
    target_days    INT  DEFAULT 14,
    daily_actions  INT  DEFAULT 5,
    status         TEXT DEFAULT 'active',  -- active/paused/completed/failed
    started_at     TIMESTAMPTZ DEFAULT now(),
    completed_at   TIMESTAMPTZ,
    last_action_at TIMESTAMPTZ,
    meta           JSONB DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_warmup_account ON account_warmup_plans(account_id);

-- Warmup action log
CREATE TABLE IF NOT EXISTS account_warmup_log (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL,
    action_type TEXT NOT NULL,  -- read_messages/join_channel/send_reaction/open_profile/search
    target      TEXT,
    success     BOOLEAN NOT NULL,
    error       TEXT,
    performed_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_warmup_log_acc ON account_warmup_log(account_id, performed_at DESC);

-- Parsed audience storage
CREATE TABLE IF NOT EXISTS parsed_audiences (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    source_type    TEXT NOT NULL,  -- channel/group/chat/comments/reactions
    source_id      BIGINT,
    source_title   TEXT,
    source_username TEXT,
    parse_run_id   BIGINT,
    tg_user_id     BIGINT NOT NULL,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    phone          TEXT,
    is_premium     BOOLEAN DEFAULT FALSE,
    is_bot         BOOLEAN DEFAULT FALSE,
    is_active      BOOLEAN,           -- active = sent message recently
    last_seen_days INT,               -- approximate days since last seen
    geo_country    TEXT,
    geo_city       TEXT,
    parsed_at      TIMESTAMPTZ DEFAULT now(),
    meta           JSONB DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_parsed_audience_dedup ON parsed_audiences(owner_id, source_id, tg_user_id);
CREATE INDEX IF NOT EXISTS idx_parsed_audience_owner ON parsed_audiences(owner_id, parsed_at DESC);

-- Parser run history
CREATE TABLE IF NOT EXISTS parser_runs (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    source_type    TEXT NOT NULL,
    source_ref     TEXT NOT NULL,   -- username or id
    source_id      BIGINT,
    parse_type     TEXT NOT NULL,   -- members/active/comments/reactions/all
    account_id     BIGINT,
    status         TEXT DEFAULT 'pending',  -- pending/running/done/failed/cancelled
    total_found    INT  DEFAULT 0,
    total_saved    INT  DEFAULT 0,
    total_skipped  INT  DEFAULT 0,
    started_at     TIMESTAMPTZ DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    error          TEXT,
    meta           JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_parser_runs_owner ON parser_runs(owner_id, started_at DESC);

-- Operation audit trail (every operation step logged)
CREATE TABLE IF NOT EXISTS operation_audit (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    operation_id  BIGINT,           -- references operation_queue.id
    account_id    BIGINT,
    action        TEXT NOT NULL,    -- create_channel/join/post/dm/etc
    target        TEXT,             -- channel username/id, user id, etc
    result        TEXT NOT NULL,    -- success/flood_wait/banned/error
    error_msg     TEXT,
    flood_wait_s  INT,
    duration_ms   INT,
    occurred_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_op_audit_owner ON operation_audit(owner_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_op_audit_op ON operation_audit(operation_id, occurred_at DESC);

-- Account capability cache (discovered capabilities per account)
CREATE TABLE IF NOT EXISTS account_capabilities (
    account_id        BIGINT PRIMARY KEY,
    owner_id          BIGINT NOT NULL,
    can_invite        BOOLEAN DEFAULT TRUE,
    can_dm            BOOLEAN DEFAULT TRUE,
    can_create_channel BOOLEAN DEFAULT TRUE,
    can_create_bot    BOOLEAN DEFAULT TRUE,
    can_set_username  BOOLEAN DEFAULT TRUE,
    is_premium        BOOLEAN DEFAULT FALSE,
    has_2fa           BOOLEAN DEFAULT FALSE,
    daily_dm_limit    INT     DEFAULT 50,
    daily_invite_limit INT    DEFAULT 200,
    last_discovery    TIMESTAMPTZ,
    restriction_flags TEXT[],  -- ['no_dm', 'limited_invite', etc]
    meta              JSONB DEFAULT '{}'
);

-- Infrastructure intelligence: daily aggregate stats per account
CREATE TABLE IF NOT EXISTS account_daily_stats (
    account_id    BIGINT NOT NULL,
    stat_date     DATE   NOT NULL DEFAULT CURRENT_DATE,
    actions_ok    INT    DEFAULT 0,
    actions_fail  INT    DEFAULT 0,
    flood_events  INT    DEFAULT 0,
    messages_sent INT    DEFAULT 0,
    invites_ok    INT    DEFAULT 0,
    joins_ok      INT    DEFAULT 0,
    PRIMARY KEY (account_id, stat_date)
);
