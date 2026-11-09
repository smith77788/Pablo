-- schema_v314: теги аккаунтов и сохранённые виды списков.
--
-- Продукт — быстрое управление десятками, сотнями и тысячами ресурсов. У
-- аккаунта была одна метка (pool), и срез «RU + на прогреве + под рассылку»
-- собрать было нечем. Теги — сколько угодно меток на аккаунт; по ним
-- фильтруют список и запускают массовые операции.
--
-- Сохранённый вид — фильтр, поиск и сортировка списка под именем
-- («Мёртвые RU»), чтобы не собирать срез заново при каждом входе.
CREATE TABLE IF NOT EXISTS account_tags (
    account_id BIGINT      NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    tag        TEXT        NOT NULL,
    owner_id   BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_account_tags_owner_tag ON account_tags (owner_id, tag);

CREATE TABLE IF NOT EXISTS saved_views (
    id         BIGSERIAL   PRIMARY KEY,
    owner_id   BIGINT      NOT NULL,
    kind       TEXT        NOT NULL,
    name       TEXT        NOT NULL,
    params     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, kind, name)
);
