-- schema_v106.sql: Content Mesh — automated content distribution network

CREATE TABLE IF NOT EXISTS content_meshes (
    id                BIGSERIAL PRIMARY KEY,
    owner_id          BIGINT NOT NULL,
    name              TEXT NOT NULL,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    source_channel    TEXT,             -- @username or -100xxx numeric ID
    source_account_id BIGINT,           -- tg_accounts.id used for reading
    last_post_id      BIGINT DEFAULT 0, -- last seen message ID in source
    delay_minutes     INT NOT NULL DEFAULT 30,
    append_text       TEXT,             -- optional suffix added to every reposted message
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS content_meshes_owner_idx ON content_meshes(owner_id);
CREATE INDEX IF NOT EXISTS content_meshes_enabled_idx ON content_meshes(enabled) WHERE enabled = TRUE;

CREATE TABLE IF NOT EXISTS mesh_targets (
    id              BIGSERIAL PRIMARY KEY,
    mesh_id         BIGINT NOT NULL REFERENCES content_meshes(id) ON DELETE CASCADE,
    target_channel  TEXT NOT NULL,   -- @username or -100xxx
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mesh_targets_mesh_idx ON mesh_targets(mesh_id);

CREATE TABLE IF NOT EXISTS mesh_queue (
    id              BIGSERIAL PRIMARY KEY,
    mesh_id         BIGINT NOT NULL REFERENCES content_meshes(id) ON DELETE CASCADE,
    target_id       BIGINT NOT NULL REFERENCES mesh_targets(id) ON DELETE CASCADE,
    source_msg_id   BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|sent|error
    scheduled_at    TIMESTAMPTZ NOT NULL,
    sent_at         TIMESTAMPTZ,
    error_msg       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mesh_queue_pending_idx ON mesh_queue(scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS mesh_queue_mesh_idx    ON mesh_queue(mesh_id);
