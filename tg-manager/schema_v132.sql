-- schema_v132: Narrative Engine — fix publishing (posts always failed: bot_id
-- was never set by the creation wizard, and there is no channel<->bot
-- relationship in the schema anyway). Publish via the channel's own Telethon
-- account instead, same pattern as self_promo.py. Needs channel_id + acc_id
-- to look up the posting account.

ALTER TABLE narrative_posts ADD COLUMN IF NOT EXISTS channel_id BIGINT;
ALTER TABLE narrative_posts ADD COLUMN IF NOT EXISTS acc_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_narrative_posts_channel ON narrative_posts(channel_id);
