CREATE TABLE IF NOT EXISTS channel_identity_history (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    old_title TEXT NOT NULL,
    new_title TEXT NOT NULL,
    source TEXT NOT NULL,
    operation_id BIGINT,
    reverted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (length(new_title) BETWEEN 1 AND 128)
);

CREATE INDEX IF NOT EXISTS idx_channel_identity_history_owner
    ON channel_identity_history(owner_id, channel_id, created_at DESC);
