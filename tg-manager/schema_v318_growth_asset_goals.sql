-- schema_v318: цели роста — канал или бот, с какого числа, до какого и к сроку.
--
-- Прежний «План роста» делил введённое число по вшитым долям и ничего не
-- сохранял: цели не было, прогресса тоже. Цель — запись: baseline (сколько
-- было в день постановки), target и deadline. По ней считаются прогресс, нужный
-- и фактический темп, прогноз и рычаги (services/growth_plan.py).
--
-- Таблица названа growth_asset_goals: growth_goals в схеме уже есть — это цели
-- организма (метрика и стратегия), у неё другие колонки, и `CREATE TABLE IF NOT
-- EXISTS growth_goals` на настоящей базе молча не создала бы ничего.
--
-- У одного канала или бота — одна активная цель: вторая размывала бы ответ на
-- вопрос «успеваем ли».
CREATE TABLE IF NOT EXISTS growth_asset_goals (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    asset_kind  TEXT        NOT NULL CHECK (asset_kind IN ('channel', 'bot')),
    asset_id    BIGINT      NOT NULL,
    title       TEXT        NOT NULL DEFAULT '',
    baseline    INT         NOT NULL,
    target      INT         NOT NULL,
    started_on  DATE        NOT NULL DEFAULT CURRENT_DATE,
    deadline    DATE        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'done', 'archived')),
    reached_on  DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_growth_asset_goals_owner ON growth_asset_goals(owner_id, deadline);
CREATE UNIQUE INDEX IF NOT EXISTS uq_growth_asset_goals_one_active
    ON growth_asset_goals(owner_id, asset_kind, asset_id) WHERE status = 'active';
