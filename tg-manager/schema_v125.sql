-- schema_v125: anti-abuse — free tier limits + global bot uniqueness
-- Free plan: 1 bot, 0 channels per account.
-- Each bot_id is UNIQUE globally (schema.sql constraint) — one bot per BotMother account worldwide.
-- Parallel multi-account abuse: 10 accounts × 1 bot = 10 bots (not worth the effort vs paid).

-- Keep trial_started_at column (harmless, was added in previous migration attempt)
ALTER TABLE platform_users
    ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ DEFAULT now();

-- Ensure global bot uniqueness index exists (already in schema.sql, but idempotent)
CREATE UNIQUE INDEX IF NOT EXISTS managed_bots_bot_id_unique ON managed_bots (bot_id);
