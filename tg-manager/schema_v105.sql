-- schema_v105.sql: Ghost Engine — autonomous background presence for TG accounts

CREATE TABLE IF NOT EXISTS ghost_profiles (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    account_id      BIGINT NOT NULL,
    personality     TEXT NOT NULL DEFAULT 'ghost',   -- ghost|watcher|active
    active_hours_start INT NOT NULL DEFAULT 9,        -- 0-23
    active_hours_end   INT NOT NULL DEFAULT 23,       -- 0-23, exclusive
    daily_cap       INT NOT NULL DEFAULT 8,
    cooldown_minutes INT NOT NULL DEFAULT 60,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, account_id)
);

CREATE INDEX IF NOT EXISTS ghost_profiles_owner_idx   ON ghost_profiles(owner_id);
CREATE INDEX IF NOT EXISTS ghost_profiles_account_idx ON ghost_profiles(account_id);
CREATE INDEX IF NOT EXISTS ghost_profiles_enabled_idx ON ghost_profiles(enabled) WHERE enabled = TRUE;

CREATE TABLE IF NOT EXISTS ghost_action_log (
    id                BIGSERIAL PRIMARY KEY,
    ghost_profile_id  BIGINT NOT NULL REFERENCES ghost_profiles(id) ON DELETE CASCADE,
    account_id        BIGINT NOT NULL,
    action_type       TEXT NOT NULL,   -- update_status|read_dialogs|react|forward_saved
    target            TEXT,
    result            TEXT NOT NULL,   -- ok|skip|error
    error_msg         TEXT,
    executed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ghost_action_log_profile_idx  ON ghost_action_log(ghost_profile_id);
CREATE INDEX IF NOT EXISTS ghost_action_log_executed_idx ON ghost_action_log(executed_at DESC);
