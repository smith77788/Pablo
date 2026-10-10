-- Multi-user Workspaces (RBAC foundation)
CREATE TABLE IF NOT EXISTS workspaces (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES platform_users(user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    plan        TEXT DEFAULT 'enterprise',
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace_members (
    id           BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL REFERENCES platform_users(user_id) ON DELETE CASCADE,
    role         TEXT NOT NULL DEFAULT 'member',  -- owner, admin, member, viewer
    invited_by   BIGINT REFERENCES platform_users(user_id),
    joined_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS workspace_invites (
    id           BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    invite_code  TEXT NOT NULL UNIQUE,
    created_by   BIGINT NOT NULL,
    uses_left    INT DEFAULT 1,
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ws_owner    ON workspaces(owner_id);
CREATE INDEX IF NOT EXISTS idx_wsm_user    ON workspace_members(user_id);
CREATE INDEX IF NOT EXISTS idx_wsm_ws      ON workspace_members(workspace_id);
CREATE INDEX IF NOT EXISTS idx_wsinv_code  ON workspace_invites(invite_code);
