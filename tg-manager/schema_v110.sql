-- schema_v110: Social Graph Engine — audience overlap + channel relationship map

CREATE TABLE IF NOT EXISTS graph_nodes (
    id           BIGSERIAL PRIMARY KEY,
    entity_id    TEXT NOT NULL UNIQUE,
    entity_type  TEXT NOT NULL DEFAULT 'channel',
    title        TEXT,
    username     TEXT,
    member_count INT NOT NULL DEFAULT 0,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS graph_nodes_entity_idx ON graph_nodes(entity_id);
CREATE INDEX IF NOT EXISTS graph_nodes_type_idx   ON graph_nodes(entity_type);

CREATE TABLE IF NOT EXISTS graph_edges (
    id          BIGSERIAL PRIMARY KEY,
    from_node   BIGINT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    to_node     BIGINT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    edge_type   TEXT NOT NULL,
    weight      FLOAT NOT NULL DEFAULT 1.0,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_node, to_node, edge_type)
);

CREATE INDEX IF NOT EXISTS graph_edges_from_idx ON graph_edges(from_node);
CREATE INDEX IF NOT EXISTS graph_edges_to_idx   ON graph_edges(to_node);

CREATE TABLE IF NOT EXISTS audience_overlaps (
    id          BIGSERIAL PRIMARY KEY,
    node_a      BIGINT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    node_b      BIGINT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    overlap_pct FLOAT NOT NULL DEFAULT 0.0,
    shared_users INT NOT NULL DEFAULT 0,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (node_a, node_b),
    CHECK (node_a < node_b)
);

CREATE INDEX IF NOT EXISTS audience_overlaps_a_idx ON audience_overlaps(node_a);
CREATE INDEX IF NOT EXISTS audience_overlaps_b_idx ON audience_overlaps(node_b);
