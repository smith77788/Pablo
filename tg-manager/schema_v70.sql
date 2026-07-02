-- schema_v70: Resource Activity Engine — активность в собственных ресурсах
-- Профили: reader | commenter | reactor | mixed
-- Адаптивный пейсинг, логирование, нишевые аккаунт-профили

CREATE TABLE IF NOT EXISTS resource_activity_sessions (
    id            SERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    name          TEXT,
    account_ids   BIGINT[] NOT NULL DEFAULT '{}',
    resource_refs TEXT[] NOT NULL DEFAULT '{}',    -- refs каналов/ботов из инфраструктуры
    profile_type  TEXT NOT NULL DEFAULT 'mixed',   -- reader | commenter | reactor | mixed
    status        TEXT NOT NULL DEFAULT 'active',  -- active | paused | completed | cancelled
    current_day   INT NOT NULL DEFAULT 0,
    target_days   INT NOT NULL DEFAULT 14,
    daily_actions INT NOT NULL DEFAULT 8,
    last_run_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS resource_activity_log (
    id            SERIAL PRIMARY KEY,
    session_id    INT NOT NULL REFERENCES resource_activity_sessions(id) ON DELETE CASCADE,
    account_id    BIGINT NOT NULL,
    action_type   TEXT NOT NULL,
    resource_ref  TEXT,
    success       BOOLEAN NOT NULL DEFAULT FALSE,
    error         TEXT,
    performed_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS account_niche_profiles (
    account_id      BIGINT PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    niche           TEXT NOT NULL DEFAULT 'general',  -- tech | news | crypto | sports | entertainment | general
    profile_type    TEXT NOT NULL DEFAULT 'reader',   -- reader | commenter | reactor | mixed
    custom_channels TEXT[] DEFAULT '{}',
    flood_wait_count INT NOT NULL DEFAULT 0,
    last_flood_at   TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ract_sessions_owner  ON resource_activity_sessions(owner_id);
CREATE INDEX IF NOT EXISTS idx_ract_sessions_status ON resource_activity_sessions(status);
CREATE INDEX IF NOT EXISTS idx_ract_log_session     ON resource_activity_log(session_id);
CREATE INDEX IF NOT EXISTS idx_niche_profiles_owner ON account_niche_profiles(owner_id);
