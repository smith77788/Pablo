-- v251: единый модуль рассылок (Кампании) — группировка дочерних операций.
--
-- Кампания — ОДНА сущность, которая веером раздаёт работу уже существующим
-- движкам (каналы → mass_publish, чаты → group_announce, боты →
-- network_broadcast, ДМ → dm_campaign) под общим идентификатором. Эти две
-- таблицы хранят саму кампанию и связь «кампания → её дочерние операции», чтобы
-- показывать единый прогресс и статус по всем ресурсам сразу, не заставляя
-- владельца прыгать по разделам.

CREATE TABLE IF NOT EXISTS broadcast_campaigns (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    name        TEXT NOT NULL DEFAULT 'Кампания',
    spec        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- единый спек (аудит/повтор)
    status      TEXT NOT NULL DEFAULT 'running',       -- running/partial/done/failed
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_owner
    ON broadcast_campaigns(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS broadcast_campaign_ops (
    campaign_id BIGINT NOT NULL REFERENCES broadcast_campaigns(id) ON DELETE CASCADE,
    op_id       BIGINT NOT NULL,
    kind        TEXT NOT NULL,                         -- channels/chats/bots/dm
    created_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (campaign_id, op_id)
);
CREATE INDEX IF NOT EXISTS idx_broadcast_campaign_ops_op
    ON broadcast_campaign_ops(op_id);
