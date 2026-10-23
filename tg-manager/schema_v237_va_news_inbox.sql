-- Durable, idempotent inbox for live editorial signals from public Telegram sources.
CREATE TABLE IF NOT EXISTS va_news_inbox (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    signal_hash TEXT NOT NULL,
    source_username TEXT NOT NULL,
    source_message_id BIGINT,
    published_at TIMESTAMPTZ NOT NULL,
    source_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'drafting', 'drafted', 'expired')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    draft_id BIGINT,
    claimed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, channel_id, signal_hash)
);

CREATE INDEX IF NOT EXISTS idx_va_news_inbox_pending
    ON va_news_inbox (owner_id, channel_id, published_at, id)
    WHERE status IN ('pending', 'drafting');
