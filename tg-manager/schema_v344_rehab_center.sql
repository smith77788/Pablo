-- schema_v344: Центр восстановления аккаунтов (services/account_rehab).
--
-- История шагов, настройки владельца, дата снятия от @SpamBot и счётчик
-- повторных блоков. Модуль заводил это только списком INLINE_MIGRATIONS в
-- mini_app_api.py (и копией в main.py) — то есть таблицы не создавала ни одна
-- миграция, и чистое окружение без старта мини-аппа ходило в несуществующие
-- таблицы (стережёт tests/test_sql_tables_exist_in_schema.py). Тот же DDL
-- оставлен и в рантайм-списках: всё идемпотентно.
ALTER TABLE account_rehab_state ADD COLUMN IF NOT EXISTS release_at TIMESTAMPTZ;
ALTER TABLE account_rehab_state ADD COLUMN IF NOT EXISTS relapses INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS account_rehab_events (
    id        BIGSERIAL PRIMARY KEY,
    acc_id    BIGINT NOT NULL,
    owner_id  BIGINT NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    step      TEXT NOT NULL,
    outcome   TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rehab_events_acc ON account_rehab_events(acc_id, at DESC);

CREATE TABLE IF NOT EXISTS rehab_settings (
    owner_id      BIGINT PRIMARY KEY,
    paused        BOOLEAN NOT NULL DEFAULT FALSE,
    auto_appeal   BOOLEAN NOT NULL DEFAULT TRUE,
    pace          TEXT NOT NULL DEFAULT 'normal',
    max_rechecks  INTEGER NOT NULL DEFAULT 8,
    rest_hours    INTEGER NOT NULL DEFAULT 24,
    notify_freed  BOOLEAN NOT NULL DEFAULT TRUE,
    notify_stuck  BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
