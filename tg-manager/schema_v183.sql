-- Воркфлоу: таблиц не существовало вовсе — экран был мёртв целиком.
--
-- ЧТО БЫЛО. Экран «Воркфлоу» в мини-аппе и восемь маршрутов API работают с
-- `workflow_definitions` и `workflow_runs`. Создавала их только функция
-- `services.workflow_engine.init_workflow_tables`, которую НИКТО не вызывает, и
-- ни одна миграция их не заводила. После полного накатывания схемы таблиц в
-- базе нет. Наружу это выходило так:
--   • список воркфлоу — вечно пустой (запрос падал, а ошибка проглатывалась);
--   • создание — 500 «relation workflow_definitions does not exist»;
--   • пауза/возобновление/удаление — 500.
-- То есть раздел выглядел работающим и не работал ни в одной своей части.
--
-- Схема повторяет init_workflow_tables, чтобы старые базы, где таблицы всё же
-- были заведены вручную, не разъехались с новыми. Отличие одно и намеренное:
-- внешний ключ с ON DELETE SET NULL — иначе удаление воркфлоу с историей
-- прогонов падало бы на нарушении ссылочной целостности.

CREATE TABLE IF NOT EXISTS workflow_definitions (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    steps       JSONB NOT NULL DEFAULT '[]',
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id            SERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    workflow_id   INTEGER REFERENCES workflow_definitions(id) ON DELETE SET NULL,
    status        TEXT DEFAULT 'pending',
    input_data    JSONB DEFAULT '{}',
    output_data   JSONB DEFAULT '{}',
    current_step  INTEGER DEFAULT 0,
    total_steps   INTEGER DEFAULT 0,
    error_message TEXT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Список воркфлоу владельца — единственный горячий запрос экрана.
CREATE INDEX IF NOT EXISTS idx_workflow_definitions_owner
    ON workflow_definitions (owner_id, name);

-- «Когда последний раз запускался» тянется подзапросом по workflow_id.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow
    ON workflow_runs (workflow_id, started_at DESC);

-- Незавершённые прогоны владельца: их отменяют при удалении воркфлоу.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_owner_status
    ON workflow_runs (owner_id, status);
