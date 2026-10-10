-- schema_v123: BotMother Nodes — Telegram Forum Workspace management

CREATE TABLE IF NOT EXISTS bm_telegram_nodes (
    id          BIGSERIAL   PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    tg_chat_id  BIGINT      NOT NULL,
    node_type   VARCHAR(32) NOT NULL,   -- proxies|accounts|tasks|alerts
    name        TEXT        NOT NULL DEFAULT '',
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(owner_id, tg_chat_id, node_type)
);

CREATE INDEX IF NOT EXISTS idx_bm_nodes_owner  ON bm_telegram_nodes(owner_id);
CREATE INDEX IF NOT EXISTS idx_bm_nodes_chat   ON bm_telegram_nodes(tg_chat_id);

CREATE TABLE IF NOT EXISTS bm_node_threads (
    id           BIGSERIAL   PRIMARY KEY,
    node_id      BIGINT      NOT NULL REFERENCES bm_telegram_nodes(id) ON DELETE CASCADE,
    tg_thread_id INT         NOT NULL,
    entity_type  VARCHAR(32) NOT NULL,   -- proxy|account|worker
    entity_id    BIGINT      NOT NULL,
    topic_name   TEXT        NOT NULL DEFAULT '',
    status       VARCHAR(16) NOT NULL DEFAULT 'open',  -- open|archived
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(node_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_bm_threads_node   ON bm_node_threads(node_id, status);
CREATE INDEX IF NOT EXISTS idx_bm_threads_entity ON bm_node_threads(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_bm_threads_lookup ON bm_node_threads(node_id, tg_thread_id);
