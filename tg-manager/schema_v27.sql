-- Asset Templates: reusable templates for bots, channels, groups and posts
CREATE TABLE IF NOT EXISTS asset_templates (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    asset_type  TEXT NOT NULL,  -- 'bot' | 'channel' | 'group' | 'post'
    name        TEXT NOT NULL,
    template    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_templates_owner
    ON asset_templates(owner_id, asset_type, created_at DESC);

-- ── v27 additions: operation queue and log ─────────────────────────────────

-- Очередь операций BotMother
CREATE TABLE IF NOT EXISTS operation_queue (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    op_type         TEXT NOT NULL,        -- mass_publish, bulk_edit, etc.
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/cancelled
    params          JSONB NOT NULL DEFAULT '{}',
    result          JSONB,
    total_items     INT DEFAULT 0,
    done_items      INT DEFAULT 0,
    error_msg       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_op_queue_owner ON operation_queue(owner_id, status);

-- Лог каждого шага операции
CREATE TABLE IF NOT EXISTS operation_log (
    id          SERIAL PRIMARY KEY,
    op_id       INT NOT NULL REFERENCES operation_queue(id) ON DELETE CASCADE,
    step_num    INT NOT NULL,
    target      TEXT,      -- channel_id / bot_id / account_id
    status      TEXT,      -- ok / skip / error
    message     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_op_log_op ON operation_log(op_id);
