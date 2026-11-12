-- schema_v360: Агент роста учится на исходах.
--
-- niche_growth_targets помнила только «здесь были» (группа и время
-- вступления). Ни запроса, по которому группа нашлась, ни исхода публикации,
-- ни судьбы поста — поэтому каждый запуск начинал с нуля: снова пробовал
-- запросы, которые ни разу не дали ни одной публикации, и не знал, где пост
-- удаляют админы. Здесь — то, на чём строится обучение.
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS keyword TEXT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS niche TEXT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS members INT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS acc_id BIGINT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS msg_id BIGINT;
-- marked (отмечена перед постом) | posted | post_failed
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS outcome_note TEXT;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;
-- Судьба поста: проверка через несколько часов (жив ли, просмотры).
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS alive BOOLEAN;
ALTER TABLE niche_growth_targets ADD COLUMN IF NOT EXISTS views INT;
CREATE INDEX IF NOT EXISTS idx_niche_growth_targets_owner_kw
    ON niche_growth_targets(owner_id, keyword);
