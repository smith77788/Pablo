CREATE TABLE IF NOT EXISTS va_learning_decisions (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    old_weights JSONB NOT NULL,
    new_weights JSONB NOT NULL,
    post_ids BIGINT[] NOT NULL,
    explanation TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_va_learning_decisions_channel
    ON va_learning_decisions(owner_id, channel_id, id DESC);
