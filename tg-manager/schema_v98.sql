-- v98: Subscription gate — mandatory channel membership before bot access
CREATE TABLE IF NOT EXISTS subscription_gate_channels (
    id              SERIAL PRIMARY KEY,
    channel_username TEXT NOT NULL,
    channel_title   TEXT NOT NULL DEFAULT '',
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
