-- Ноды-комьюнити: участники-флот и роли (оживление + модерация нодой).
-- role: member (просто присутствие) | moderator | admin.
CREATE TABLE IF NOT EXISTS community_node_members (
    id          BIGSERIAL   PRIMARY KEY,
    node_id     BIGINT      NOT NULL REFERENCES community_nodes(id) ON DELETE CASCADE,
    account_id  BIGINT      NOT NULL,
    role        VARCHAR(16) NOT NULL DEFAULT 'member',   -- member|moderator|admin
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (node_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_community_members_node ON community_node_members(node_id, role);
