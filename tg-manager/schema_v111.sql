-- schema_v111: Compute API — per-user API key management

CREATE TABLE IF NOT EXISTS api_keys (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL,
    key_hash       TEXT NOT NULL UNIQUE,
    key_prefix     TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT 'Default',
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at   TIMESTAMPTZ,
    requests_total BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS api_keys_user_idx ON api_keys(user_id) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS api_keys_hash_idx ON api_keys(key_hash) WHERE is_active = TRUE;
