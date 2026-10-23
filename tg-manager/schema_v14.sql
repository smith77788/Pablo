-- Admin panel: state machine for multi-step admin inputs
CREATE TABLE IF NOT EXISTS admin_state (
    admin_id   BIGINT PRIMARY KEY,
    state      TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Blocked platform users (denied access to management bot)
CREATE TABLE IF NOT EXISTS blocked_users (
    user_id    BIGINT PRIMARY KEY,
    blocked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Platform users tracking (first-seen log)
CREATE TABLE IF NOT EXISTS platform_users (
    user_id      BIGINT PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_blocked   BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_platform_users_seen ON platform_users(first_seen);
