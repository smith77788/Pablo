-- schema_v91: reg_check_cache — кэш дат регистрации/создания Telegram-сущностей
CREATE TABLE IF NOT EXISTS reg_check_cache (
    entity_id   BIGINT  NOT NULL,
    entity_type TEXT    NOT NULL,
    entity_name TEXT,
    username    TEXT,
    reg_date    TIMESTAMPTZ,
    method      TEXT    NOT NULL DEFAULT 'id_interpolation',
    checked_by  BIGINT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_id, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_reg_check_cache_checked_at
    ON reg_check_cache(checked_at DESC);
