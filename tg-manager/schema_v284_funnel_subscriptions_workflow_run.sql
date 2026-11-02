-- schema_v284: подписка воронки, переданная прогону движка сценариев.
--
-- funnel_subscriptions.workflow_run_id и auto_funnel_runs.workflow_run_id —
-- id прогона в workflow_runs, который ведёт эту подписку (см. schema_v283 и
-- services/legacy_funnels.py). Старые исполнители такие строки не берут;
-- пустое значение — строка у старого исполнителя.

ALTER TABLE funnel_subscriptions ADD COLUMN IF NOT EXISTS workflow_run_id BIGINT;
ALTER TABLE auto_funnel_runs     ADD COLUMN IF NOT EXISTS workflow_run_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_funnel_subs_workflow_run
    ON funnel_subscriptions (workflow_run_id) WHERE workflow_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_auto_funnel_runs_workflow_run
    ON auto_funnel_runs (workflow_run_id) WHERE workflow_run_id IS NOT NULL;
