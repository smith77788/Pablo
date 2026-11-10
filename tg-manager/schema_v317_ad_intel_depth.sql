-- schema_v317: «Разведка рекламы» — честные метрики и сбор без дублей.
--
-- 1. ad_placements.er_rate хранил ОХВАТ (просмотры ÷ подписчики), а экран
--    подписывал его как ER. Охват и вовлечённость теперь — разные колонки:
--    reach_rate (просмотры ÷ подписчики) и engagement_rate
--    ((реакции + репосты + комментарии) ÷ просмотры). er_rate оставлен для
--    старых читателей и пишется равным reach_rate.
-- 2. Повторный скан писал те же посты в ad_posts_log ещё раз. Теперь у поста
--    есть msg_id, и пара (placement_id, msg_id) уникальна.
-- 3. Рекламодатель получил тип (канал, бот, сайт, закрытая ссылка), число
--    каналов, где он размещался, и суммарные просмотры. placements_count
--    больше не прибавляется на каждом скане, а пересчитывается по журналу.
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS reach_rate FLOAT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS engagement_rate FLOAT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS posts_scanned INT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS posts_per_day FLOAT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS ads_per_week FLOAT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS ad_share FLOAT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS ad_price_rub INT DEFAULT 0;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS fraud_flags TEXT[] DEFAULT ARRAY[]::TEXT[];
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS auto_rescan BOOLEAN DEFAULT TRUE;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE ad_placements ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS msg_id BIGINT;
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS post_date TIMESTAMPTZ;
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS advertiser_kind VARCHAR(16);
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS ad_link TEXT;
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS has_erid BOOLEAN DEFAULT FALSE;
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS confidence SMALLINT DEFAULT 0;
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS reasons TEXT[] DEFAULT ARRAY[]::TEXT[];
ALTER TABLE ad_posts_log ADD COLUMN IF NOT EXISTS post_er FLOAT DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ad_posts_placement_msg
    ON ad_posts_log(placement_id, msg_id) WHERE msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ad_posts_owner_adv
    ON ad_posts_log(owner_id, advertiser_username);
CREATE INDEX IF NOT EXISTS idx_ad_posts_owner_date
    ON ad_posts_log(owner_id, post_date DESC);

ALTER TABLE ad_advertisers ADD COLUMN IF NOT EXISTS kind VARCHAR(16);
ALTER TABLE ad_advertisers ADD COLUMN IF NOT EXISTS channels_count INT DEFAULT 0;
ALTER TABLE ad_advertisers ADD COLUMN IF NOT EXISTS total_views BIGINT DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_ad_placements_rescan
    ON ad_placements(last_scanned_at) WHERE auto_rescan;
