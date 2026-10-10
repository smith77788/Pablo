-- Организм: общая нервная система и память, связывающие разрозненные модули.
-- organism_events — журнал событий (что произошло: op_done, intent, ban, joined…):
--   и память, и durable-шина между процессами.
-- organism_state — durable контекст владельца (активная цель, отклонённые
--   подсказки, время последнего нуджа) — общее состояние организма.
CREATE TABLE IF NOT EXISTS organism_events (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_org_events_owner ON organism_events(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_org_events_kind  ON organism_events(owner_id, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS organism_state (
    owner_id    BIGINT NOT NULL,
    key         TEXT NOT NULL,
    value       JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, key)
);
