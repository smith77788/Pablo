-- schema_v108.sql: Auto-Funnel — automated message sequences for bot audience segments

CREATE TABLE IF NOT EXISTS auto_funnels (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    name            TEXT NOT NULL,
    bot_id          BIGINT NOT NULL,          -- which bot sends the messages
    target_segment  TEXT NOT NULL DEFAULT 'all',  -- all|new_7d|new_30d|inactive_30d
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS auto_funnels_owner_idx ON auto_funnels(owner_id);

CREATE TABLE IF NOT EXISTS auto_funnel_steps (
    id              BIGSERIAL PRIMARY KEY,
    funnel_id       BIGINT NOT NULL REFERENCES auto_funnels(id) ON DELETE CASCADE,
    step_num        INT NOT NULL,
    delay_hours     INT NOT NULL DEFAULT 0,   -- delay from funnel launch (not previous step)
    message_text    TEXT NOT NULL,
    button_text     TEXT,                     -- optional inline button label
    button_url      TEXT,                     -- optional inline button URL
    UNIQUE (funnel_id, step_num)
);

CREATE INDEX IF NOT EXISTS auto_funnel_steps_funnel_idx ON auto_funnel_steps(funnel_id);

CREATE TABLE IF NOT EXISTS auto_funnel_runs (
    id              BIGSERIAL PRIMARY KEY,
    funnel_id       BIGINT NOT NULL REFERENCES auto_funnels(id) ON DELETE CASCADE,
    bot_id          BIGINT NOT NULL,
    user_id         BIGINT NOT NULL,          -- Telegram user_id to send to
    next_step_num   INT NOT NULL DEFAULT 1,
    next_send_at    TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',  -- active|completed|stopped|error
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (funnel_id, user_id)
);

CREATE INDEX IF NOT EXISTS auto_funnel_runs_pending_idx ON auto_funnel_runs(next_send_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS auto_funnel_runs_funnel_idx  ON auto_funnel_runs(funnel_id);
