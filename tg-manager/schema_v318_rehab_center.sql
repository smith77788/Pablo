-- schema_v318: восстановление аккаунтов — история шагов, настройки владельца,
-- дата снятия от @SpamBot и счётчик повторных блоков.
--
-- Модуль был чёрным ящиком с одной строкой «заметка»: что и когда делала
-- автоматика, увидеть было нельзя. Темп, лимиты и уведомления были зашиты в
-- код. Временный блок с названной датой снятия перепроверялся вслепую и мог
-- уйти «на разбор» раньше этой даты. А аккаунт, однажды восстановленный и
-- заблокированный снова, не подхватывался никогда: строка уже стояла в
-- 'freed', и заведение делало ON CONFLICT DO NOTHING.
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
