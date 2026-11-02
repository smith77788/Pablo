-- schema_v271: цель сценария, журнал шагов прогона, источник контакта.
--
-- ЧТО БЫЛО. Сценарий не знал своей цели: человек оплатил или получил тег
-- «купил», а дожим продолжал слать сообщения. По шагам не было ни одной
-- цифры: сколько людей дошло до шага, сколько нажало, сколько оплатило и на
-- какую сумму. Что случилось с конкретным человеком, видно было только по
-- последнему статусу прогона.
--
-- ЧТО ЗДЕСЬ.
--   * workflow_definitions.goal_type / goal_value — цель: тег, оплата или
--     любое событие шины. Достигнута — прогон завершается сам.
--   * workflow_runs.source — откуда пришёл человек (параметр ссылки /start),
--     goal_reached_at — когда цель достигнута.
--   * workflow_step_log — одна строка на исполненный шаг: из неё строятся и
--     журнал прогона, и счётчики на каждом шаге, и выручка сценария.

ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS goal_type  TEXT;
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS goal_value TEXT;

ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS source          TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS goal_reached_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS workflow_step_log (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT      NOT NULL,
    workflow_id BIGINT      NOT NULL,
    owner_id    BIGINT      NOT NULL,
    step_idx    INTEGER     NOT NULL,
    step_type   TEXT        NOT NULL,
    outcome     TEXT        NOT NULL,
    detail      TEXT,
    amount      BIGINT,
    currency    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workflow_step_log_run
    ON workflow_step_log (run_id, id);
CREATE INDEX IF NOT EXISTS idx_workflow_step_log_wf
    ON workflow_step_log (workflow_id, step_idx);
CREATE INDEX IF NOT EXISTS idx_workflow_step_log_created
    ON workflow_step_log (created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_definitions_goal
    ON workflow_definitions (owner_id) WHERE goal_type IS NOT NULL;
