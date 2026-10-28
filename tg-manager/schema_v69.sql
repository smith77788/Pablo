-- schema_v69: Warmup Sessions — multi-account multi-target warmup
-- Новый подход: несколько рабочих аккаунтов → конкретные цели (каналы/боты/группы)

CREATE TABLE IF NOT EXISTS warmup_sessions (
    id            SERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    name          TEXT,
    account_ids   BIGINT[] NOT NULL DEFAULT '{}',
    target_type   TEXT NOT NULL DEFAULT 'infra',   -- 'infra' | 'manual'
    target_refs   TEXT[] NOT NULL DEFAULT '{}',    -- usernames / channel_ids / bot usernames
    plan_type     TEXT NOT NULL DEFAULT 'standard', -- 'gentle' | 'standard' | 'aggressive'
    status        TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'paused' | 'completed' | 'cancelled'
    current_day   INT NOT NULL DEFAULT 0,
    target_days   INT NOT NULL DEFAULT 14,
    daily_actions INT NOT NULL DEFAULT 10,
    started_at    TIMESTAMPTZ DEFAULT NOW(),
    last_run_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS warmup_session_log (
    id            SERIAL PRIMARY KEY,
    session_id    INT NOT NULL REFERENCES warmup_sessions(id) ON DELETE CASCADE,
    account_id    BIGINT NOT NULL,
    action_type   TEXT NOT NULL,
    target        TEXT,
    success       BOOLEAN NOT NULL DEFAULT FALSE,
    error         TEXT,
    performed_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_warmup_sessions_owner  ON warmup_sessions(owner_id);
CREATE INDEX IF NOT EXISTS idx_warmup_sessions_status ON warmup_sessions(status);
CREATE INDEX IF NOT EXISTS idx_warmup_session_log_sid ON warmup_session_log(session_id);
