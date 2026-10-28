-- v19: Managed channels from connected accounts
CREATE TABLE IF NOT EXISTS managed_channels (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    acc_id      INTEGER NOT NULL,
    channel_id  BIGINT NOT NULL,
    title       TEXT,
    username    TEXT,
    added_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(owner_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_managed_channels_owner ON managed_channels(owner_id);
