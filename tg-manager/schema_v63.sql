-- Schema v63: persistent BotMother AI memory

CREATE TABLE IF NOT EXISTS botmother_memory (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    source TEXT NOT NULL DEFAULT 'manual',
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_botmother_memory_owner_updated
    ON botmother_memory(owner_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_botmother_memory_owner_kind
    ON botmother_memory(owner_id, kind);

CREATE INDEX IF NOT EXISTS idx_botmother_memory_tags
    ON botmother_memory USING GIN(tags);
