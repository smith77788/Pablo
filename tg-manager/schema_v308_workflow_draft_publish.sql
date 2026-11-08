-- schema_v308: черновик и публикация сценария, версия на каждом прогоне.
--
-- ЧТО БЫЛО. Правка шагов включённого сценария сразу доставалась всем, кто
-- входил в него после правки: полусобранная ветка уходила живым людям, пока
-- владелец ещё её дописывал. Какую версию шагов прошёл человек, не было
-- видно.
--
-- ЧТО ЗДЕСЬ.
--   * workflow_definitions.draft_steps — черновик. Правка включённого
--     сценария идёт в него, а люди проходят опубликованные `steps`, пока
--     владелец не нажмёт «Опубликовать». NULL — черновика нет.
--   * workflow_definitions.steps_version / published_at — номер и время
--     последней публикации.
--   * workflow_runs.steps_version — какую версию шагов проходит прогон.

ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS draft_steps   JSONB;
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS steps_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS published_at  TIMESTAMPTZ;
ALTER TABLE workflow_runs        ADD COLUMN IF NOT EXISTS steps_version INTEGER;
