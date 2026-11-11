-- schema_v319: радар ниши — каталог внешних ресурсов и присутствие в них.
--
-- Задумка владельца: агент находит МАКСИМУМ ресурсов по нише (группы, каналы и
-- их чаты обсуждений), флот вступает в них и ведёт там продвижение —
-- комментарии, реакции и другие полезные действия. Прежний Growth Agent брал
-- за запуск пять групп, ничего о них не помнил, кроме «уже постили», а
-- ИИ-комментирование работало только по каналам, вписанным руками.
--
-- Три таблицы: кампания (ниша и правила), каталог найденного (asset) и журнал
-- действий. Каталог — главное: он копится между запусками, поэтому поиск не
-- начинается каждый раз с нуля, а присутствие знает, где мы уже были.
CREATE TABLE IF NOT EXISTS niche_campaigns (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT      NOT NULL,
    title         TEXT        NOT NULL DEFAULT '',
    niche         TEXT        NOT NULL,
    geo           TEXT        NOT NULL DEFAULT '',
    keywords      TEXT[]      NOT NULL DEFAULT '{}',
    promo_text    TEXT        NOT NULL DEFAULT '',
    promo_link    TEXT        NOT NULL DEFAULT '',
    tone          TEXT        NOT NULL DEFAULT 'neutral',
    min_members   INT         NOT NULL DEFAULT 100,
    status        TEXT        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused', 'archived')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_scan_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_niche_campaigns_owner
    ON niche_campaigns(owner_id, created_at DESC);

-- Найденный ресурс. tg_id уникален в пределах владельца: один и тот же чат
-- находится разными ключами и из разных источников, и дубль в каталоге
-- означал бы второе вступление и второй комментарий в то же место.
CREATE TABLE IF NOT EXISTS niche_assets (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT      NOT NULL,
    campaign_id   BIGINT      REFERENCES niche_campaigns(id) ON DELETE SET NULL,
    tg_id         BIGINT      NOT NULL,
    access_hash   BIGINT      NOT NULL DEFAULT 0,
    kind          TEXT        NOT NULL DEFAULT 'group'
                  CHECK (kind IN ('group', 'channel')),
    username      TEXT        NOT NULL DEFAULT '',
    title         TEXT        NOT NULL DEFAULT '',
    members       INT         NOT NULL DEFAULT 0,
    source        TEXT        NOT NULL DEFAULT 'search',
    keyword       TEXT        NOT NULL DEFAULT '',
    linked_chat_id BIGINT,
    score         INT         NOT NULL DEFAULT 0,
    status        TEXT        NOT NULL DEFAULT 'found'
                  CHECK (status IN ('found', 'skipped', 'joined', 'failed', 'banned', 'left')),
    skip_reason   TEXT        NOT NULL DEFAULT '',
    joined_acc_id BIGINT,
    joined_at     TIMESTAMPTZ,
    last_action_at TIMESTAMPTZ,
    actions_done  INT         NOT NULL DEFAULT 0,
    found_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, tg_id)
);
CREATE INDEX IF NOT EXISTS idx_niche_assets_campaign
    ON niche_assets(campaign_id, status, score DESC);
CREATE INDEX IF NOT EXISTS idx_niche_assets_owner_status
    ON niche_assets(owner_id, status);

-- Журнал действий: что именно делали в ресурсе и чем кончилось.
CREATE TABLE IF NOT EXISTS niche_asset_actions (
    id         BIGSERIAL PRIMARY KEY,
    asset_id   BIGINT      NOT NULL REFERENCES niche_assets(id) ON DELETE CASCADE,
    owner_id   BIGINT      NOT NULL,
    op_id      BIGINT,
    acc_id     BIGINT,
    kind       TEXT        NOT NULL,
    status     TEXT        NOT NULL DEFAULT 'ok',
    detail     TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_niche_actions_asset
    ON niche_asset_actions(asset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_niche_actions_owner
    ON niche_asset_actions(owner_id, created_at DESC);
