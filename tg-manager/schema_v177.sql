-- Ноды-комьюнити (mini-Discord). Отдельно от инфра-нод (bm_telegram_nodes),
-- чтобы не смешивать назначения: инфра-ноды — дашборд ресурсов, комьюнити-ноды —
-- сообщество для АУДИТОРИИ. Форум-супергруппа = нода, форум-топики = каналы.
CREATE TABLE IF NOT EXISTS community_nodes (
    id           BIGSERIAL   PRIMARY KEY,
    owner_id     BIGINT      NOT NULL,
    tg_chat_id   BIGINT      NOT NULL,
    title        TEXT        NOT NULL DEFAULT '',
    description  TEXT        NOT NULL DEFAULT '',
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, tg_chat_id)
);
CREATE INDEX IF NOT EXISTS idx_community_nodes_owner ON community_nodes(owner_id, is_active);

CREATE TABLE IF NOT EXISTS community_channels (
    id           BIGSERIAL   PRIMARY KEY,
    node_id      BIGINT      NOT NULL REFERENCES community_nodes(id) ON DELETE CASCADE,
    tg_topic_id  INT         NOT NULL,
    name         TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (node_id, tg_topic_id)
);
CREATE INDEX IF NOT EXISTS idx_community_channels_node ON community_channels(node_id);
