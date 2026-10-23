-- v156: таблицы Network Builder (Конструктор сетей).
--
-- services/network_builder.py::init_network_tables создаёт эти таблицы, но
-- init_network_tables НЕ вызывается НИГДЕ (мёртвый код) → таблиц в БД не было →
-- экран «Конструктор сетей» падал с `relation "network_instances" does not exist`
-- (и все network-эндпоинты mini_app: get_instances/create_instance/nodes/edges).
--
-- Переношу DDL в миграцию (авто-применяется db.create_pool), 1:1 со схемой из
-- init_network_tables. Имена уникальны — коллизий с существующими таблицами нет.
-- Порядок важен из-за FK: templates → instances → nodes → edges.

CREATE TABLE IF NOT EXISTS network_templates (
    id            SERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,
    template_type TEXT NOT NULL DEFAULT 'channel_group',
    nodes         JSONB NOT NULL DEFAULT '[]',
    edges         JSONB NOT NULL DEFAULT '[]',
    settings      JSONB DEFAULT '{}',
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_instances (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    template_id INTEGER REFERENCES network_templates(id),
    name        TEXT NOT NULL,
    status      TEXT DEFAULT 'draft',
    nodes       JSONB NOT NULL DEFAULT '[]',
    edges       JSONB NOT NULL DEFAULT '[]',
    stats       JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    launched_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_network_instances_owner ON network_instances(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS network_nodes (
    id          SERIAL PRIMARY KEY,
    instance_id INTEGER REFERENCES network_instances(id) ON DELETE CASCADE,
    node_type   TEXT NOT NULL,
    ref_id      BIGINT,
    label       TEXT,
    config      JSONB DEFAULT '{}',
    status      TEXT DEFAULT 'pending',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_network_nodes_instance ON network_nodes(instance_id);

CREATE TABLE IF NOT EXISTS network_edges (
    id             SERIAL PRIMARY KEY,
    instance_id    INTEGER REFERENCES network_instances(id) ON DELETE CASCADE,
    source_node_id INTEGER REFERENCES network_nodes(id),
    target_node_id INTEGER REFERENCES network_nodes(id),
    edge_type      TEXT NOT NULL DEFAULT 'admin',
    config         JSONB DEFAULT '{}',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_network_edges_instance ON network_edges(instance_id);
