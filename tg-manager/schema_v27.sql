-- Asset Templates: reusable templates for bots, channels, groups and posts
CREATE TABLE IF NOT EXISTS asset_templates (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    asset_type  TEXT NOT NULL,  -- 'bot' | 'channel' | 'group' | 'post'
    name        TEXT NOT NULL,
    template    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_templates_owner
    ON asset_templates(owner_id, asset_type, created_at DESC);
