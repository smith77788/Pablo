-- Shadowban / restriction detection log
CREATE TABLE IF NOT EXISTS restriction_events (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    account_id      BIGINT REFERENCES tg_accounts(id) ON DELETE SET NULL,
    bot_id          BIGINT REFERENCES managed_bots(bot_id) ON DELETE SET NULL,
    event_type      TEXT NOT NULL,  -- 'search_drop', 'dm_restricted', 'invite_degraded', 'account_restricted'
    severity        TEXT NOT NULL DEFAULT 'warning',  -- 'info', 'warning', 'critical'
    details         JSONB,
    alerted_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_restriction_events_owner
    ON restriction_events(owner_id, created_at DESC);

-- Cooldown to prevent repeated alerts (one alert per event_type per 24h)
CREATE TABLE IF NOT EXISTS restriction_alert_cooldown (
    owner_id    BIGINT NOT NULL,
    event_type  TEXT NOT NULL,
    entity_id   BIGINT NOT NULL DEFAULT 0,  -- account_id or bot_id
    last_alerted TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, event_type, entity_id)
);
