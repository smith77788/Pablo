-- ClickHouse analytics schema for Telegram SaaS platform

CREATE DATABASE IF NOT EXISTS tgplatform;

USE tgplatform;

-- ─── RAW EVENTS ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS events (
    tenant_id     String,
    bot_id        String,
    user_id       String,
    telegram_id   Int64,
    event_type    LowCardinality(String),
    session_id    String,
    conversation_id String DEFAULT '',
    properties    String DEFAULT '{}',  -- JSON
    timestamp     DateTime64(3, 'UTC'),
    date          Date DEFAULT toDate(timestamp)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (tenant_id, bot_id, timestamp, user_id)
TTL date + INTERVAL 2 YEAR
SETTINGS index_granularity = 8192;

-- ─── BOT DAILY METRICS ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bot_daily_metrics (
    tenant_id     String,
    bot_id        String,
    date          Date,
    new_users     UInt32 DEFAULT 0,
    active_users  UInt32 DEFAULT 0,
    messages_in   UInt32 DEFAULT 0,
    messages_out  UInt32 DEFAULT 0,
    conversations_opened  UInt32 DEFAULT 0,
    conversations_resolved UInt32 DEFAULT 0,
    broadcasts_sent UInt32 DEFAULT 0,
    avg_response_seconds Float32 DEFAULT 0,
    d1_retained   UInt32 DEFAULT 0,
    d7_retained   UInt32 DEFAULT 0,
    d30_retained  UInt32 DEFAULT 0
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (tenant_id, bot_id, date);

-- ─── USER SESSIONS ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_sessions (
    tenant_id    String,
    bot_id       String,
    user_id      String,
    telegram_id  Int64,
    session_id   String,
    started_at   DateTime64(3, 'UTC'),
    ended_at     DateTime64(3, 'UTC') DEFAULT '1970-01-01 00:00:00',
    duration_sec UInt32 DEFAULT 0,
    message_count UInt16 DEFAULT 0,
    date         Date DEFAULT toDate(started_at)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (tenant_id, bot_id, started_at, user_id)
TTL date + INTERVAL 1 YEAR;

-- ─── SEARCH / GROWTH METRICS ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS growth_events (
    tenant_id   String,
    bot_id      String,
    event_type  LowCardinality(String), -- search_impression|search_click|join|leave
    source      LowCardinality(String), -- search|link|qr|forward
    keyword     String DEFAULT '',
    position    UInt8 DEFAULT 0,
    user_id     String DEFAULT '',
    timestamp   DateTime64(3, 'UTC'),
    date        Date DEFAULT toDate(timestamp)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (tenant_id, bot_id, timestamp)
TTL date + INTERVAL 1 YEAR;

-- ─── BROADCAST ANALYTICS ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS broadcast_events (
    tenant_id    String,
    bot_id       String,
    broadcast_id String,
    telegram_id  Int64,
    event_type   LowCardinality(String), -- sent|delivered|read|clicked|failed
    timestamp    DateTime64(3, 'UTC'),
    date         Date DEFAULT toDate(timestamp)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (tenant_id, broadcast_id, timestamp)
TTL date + INTERVAL 1 YEAR;

-- ─── MATERIALIZED VIEWS ──────────────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_bot_daily_metrics
TO bot_daily_metrics
AS SELECT
    tenant_id,
    bot_id,
    toDate(timestamp) AS date,
    countIf(event_type = 'bot_start') AS new_users,
    uniqIf(user_id, event_type IN ('message_received', 'message_sent', 'bot_start')) AS active_users,
    countIf(event_type = 'message_received') AS messages_in,
    countIf(event_type = 'message_sent') AS messages_out,
    countIf(event_type = 'conversation_opened') AS conversations_opened,
    countIf(event_type = 'conversation_resolved') AS conversations_resolved
FROM events
GROUP BY tenant_id, bot_id, date;
