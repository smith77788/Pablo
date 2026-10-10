-- schema_v141: история численности участников каналов для статистики роста.
CREATE TABLE IF NOT EXISTS channel_member_history (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT  NOT NULL,
    channel_id    BIGINT  NOT NULL,
    members_count INTEGER NOT NULL DEFAULT 0,
    captured_on   DATE    NOT NULL DEFAULT CURRENT_DATE,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, channel_id, captured_on)
);
CREATE INDEX IF NOT EXISTS idx_cmh_owner_channel
    ON channel_member_history(owner_id, channel_id, captured_on DESC);
