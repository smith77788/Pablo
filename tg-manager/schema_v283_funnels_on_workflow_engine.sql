-- schema_v283: обе старые воронки исполняет движок сценариев.
--
-- ЧТО БЫЛО. Три исполнителя цепочек жили каждый сам по себе: воронки
-- (funnels, funnel_runner), автоворонки (auto_funnels, auto_funnel) и
-- сценарии (workflow_definitions, workflow_runtime). У старых двух не было
-- ни цели, ни журнала шагов, ни выручки, ни умной задержки, ни A/B, и
-- каждая чинилась отдельно (двойная отправка, тихий обрыв на блипе).
--
-- ЧТО ЗДЕСЬ. Старые таблицы остаются источником правды для своих экранов
-- (там их редактируют), а исполняет их движок сценариев:
--   * workflow_definitions.legacy_kind / legacy_id — сценарий-зеркало
--     воронки ('funnel') или автоворонки ('auto_funnel'). Шаги зеркала
--     пересобираются из старых таблиц (services/legacy_funnels.py).
--   * funnel_subscriptions.workflow_run_id и auto_funnel_runs.workflow_run_id —
--     подписка передана прогону движка. Старые исполнители такие строки не
--     берут. Пустое значение — строка ещё (или снова) у старого исполнителя.
-- Откат без потери состояния: FUNNELS_ENGINE=legacy возвращает живые
-- прогоны в подписки на тот же шаг и время (legacy_funnels.release).

ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS legacy_kind TEXT;
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS legacy_id   BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_definitions_legacy
    ON workflow_definitions (legacy_kind, legacy_id) WHERE legacy_kind IS NOT NULL;

-- Привязка подписок к прогонам — в schema_v284 (другие таблицы: тест
-- таблиц сценариев применяет только миграции, которые их касаются).
