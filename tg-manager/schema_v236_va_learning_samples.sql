-- Fixed-age observations are separate from the continuously refreshed counters.
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS learning_views BIGINT;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS learning_reactions BIGINT;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS learning_forwards BIGINT;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS learning_sampled_at TIMESTAMPTZ;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS learned_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_va_posts_unlearned
    ON va_channel_posts(owner_id, channel_key, published_at)
    WHERE learning_sampled_at IS NOT NULL AND learned_at IS NULL;
