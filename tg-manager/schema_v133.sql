-- schema_v133: Growth Agent — remember which groups an owner already targeted
-- so repeat runs don't rejoin/repost into the same groups (niche_searcher's
-- search_niche_groups() already supports an exclude_ids filter, but nothing
-- ever populated it — every run searched fresh with no memory of past runs).

CREATE TABLE IF NOT EXISTS niche_growth_targets (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    group_id   BIGINT NOT NULL,
    title      TEXT,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(owner_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_niche_growth_targets_owner ON niche_growth_targets(owner_id);
