-- schema_v263: исполнитель сценариев и общая шина событий автоматизации.
--
-- ЧТО БЫЛО. Конструктор сценариев (workflow_definitions) сохранял шаги, а
-- «запуск» только заводил строку workflow_runs со статусом running: шаги не
-- выполнял никто, запуска по событию не было. Правила, воронки и CRM жили
-- каждый сам по себе, и ни одно событие продукта (вход в бота, тег, оплата,
-- смена этапа сделки) не могло запустить цепочку действий.
--
-- ЧТО ЗДЕСЬ.
--   * automation_events — общая шина: любое значимое событие пишется сюда
--     одной строкой. Читатели идут по курсору (id > последнего прочитанного),
--     у каждого свой курсор в automation_cursors, поэтому исполнитель
--     сценариев и доставка исходящих вебхуков не мешают друг другу.
--     Имена колонок event_type / data / owner_id — общий контракт с модулем
--     интеграций, не переименовывать.
--   * workflow_definitions.trigger_* — когда сценарий запускается сам.
--   * workflow_runs — прогон теперь привязан к человеку (bot_id, chat_id),
--     хранит указатель шага, переменные, ожидание ответа/кнопки/оплаты и
--     снимок шагов на момент старта (правка сценария не ломает идущие прогоны).
--
-- Поля контакта и оплата заказов в чате — в schema_v265 (другие таблицы).

CREATE TABLE IF NOT EXISTS automation_events (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    event_type  TEXT        NOT NULL,
    bot_id      BIGINT,
    chat_id     BIGINT,
    data        JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_automation_events_owner
    ON automation_events (owner_id, id);
CREATE INDEX IF NOT EXISTS idx_automation_events_created
    ON automation_events (created_at);

CREATE TABLE IF NOT EXISTS automation_cursors (
    consumer    TEXT        PRIMARY KEY,
    last_id     BIGINT      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Триггер сценария: NULL/'manual' — только ручной запуск, 'start' — /start
-- (trigger_value — параметр диплинка, пусто = любой), 'keyword' — сообщение
-- содержит слово, 'tag_added' — человеку поставили тег, 'event' — любое
-- событие шины по его типу (trigger_value = event_type).
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS trigger_type  TEXT;
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS trigger_value TEXT;
-- Повторный вход: FALSE — человек проходит сценарий один раз.
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS allow_reentry BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS bot_id         BIGINT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS chat_id        BIGINT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS steps_snapshot JSONB;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS vars           JSONB NOT NULL DEFAULT '{}';
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS next_run_at    TIMESTAMPTZ;
-- Счётчик исполненных шагов: шаг «занимается» условным UPDATE по нему ДО
-- отправки, поэтому два исполнителя (или повтор после сбоя) не отправят одно
-- сообщение дважды. С переходами (goto) один шаг может исполняться повторно,
-- поэтому счётчик, а не номер шага.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS exec_seq       INTEGER NOT NULL DEFAULT 0;
-- Ожидание: 'button' | 'reply' | 'payment', wait_until — тайм-аут ожидания.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS wait_kind      TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS wait_until     TIMESTAMPTZ;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS claimed_until  TIMESTAMPTZ;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS fail_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS trigger_event_id BIGINT;

-- Один живой прогон сценария на человека: повторный /start не запускает
-- вторую параллельную цепочку тех же сообщений.
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_runs_live
    ON workflow_runs (workflow_id, bot_id, chat_id)
    WHERE status IN ('running', 'waiting') AND chat_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_runs_due
    ON workflow_runs (next_run_at) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_workflow_runs_waiting
    ON workflow_runs (bot_id, chat_id) WHERE status = 'waiting';
CREATE INDEX IF NOT EXISTS idx_workflow_runs_wait_until
    ON workflow_runs (wait_until) WHERE status = 'waiting';
CREATE INDEX IF NOT EXISTS idx_workflow_definitions_trigger
    ON workflow_definitions (owner_id, trigger_type) WHERE is_active AND trigger_type IS NOT NULL;
