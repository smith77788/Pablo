-- schema_v159: Global Presence → генератор инфраструктуры.
--
-- План перестаёт быть «один паттерн × список городов» и становится описанием
-- ПРОЕКТА: уровни географии, набор ролей (тематик), пулы шаблонов и стиль
-- аватаров. Цель, соответственно, несёт свою роль, своё описание и seed
-- аватара — то есть всё, что нужно исполнителю, чтобы воспроизвести ровно тот
-- объект, который пользователь видел в предпросмотре.
--
-- Все колонки добавляются как NULLable: старые планы продолжают исполняться по
-- прежнему пути (name_pattern/username_pattern), новые — по генератору.

-- ── План = проект ───────────────────────────────────────────────────────────
ALTER TABLE global_presence_plans
    ADD COLUMN IF NOT EXISTS project_name    TEXT,
    -- Роли (тематики) и уровни географии: JSONB-массивы строк.
    ADD COLUMN IF NOT EXISTS roles           JSONB,
    ADD COLUMN IF NOT EXISTS levels          JSONB,
    -- Пулы шаблонов. Пустой/NULL — берём библиотечные пулы роли.
    ADD COLUMN IF NOT EXISTS name_pool       JSONB,
    ADD COLUMN IF NOT EXISTS username_pool   JSONB,
    ADD COLUMN IF NOT EXISTS about_pool      JSONB,
    ADD COLUMN IF NOT EXISTS avatar_style    TEXT,
    -- Зерно генерации. Хранится, потому что от него зависит ВСЁ содержимое
    -- плана: без него повторная сборка (ретрай, догенерация) дала бы другие
    -- имена и аватары, чем показал предпросмотр.
    ADD COLUMN IF NOT EXISTS plan_seed       BIGINT;

-- ── Цель = объект ───────────────────────────────────────────────────────────
ALTER TABLE global_presence_targets
    ADD COLUMN IF NOT EXISTS role            TEXT,
    ADD COLUMN IF NOT EXISTS level           TEXT,
    -- Описание генерируется вместе с названием. Раньше исполнитель подставлял
    -- захардкоженную строку, одинаковую для всей сети.
    ADD COLUMN IF NOT EXISTS planned_about   TEXT,
    ADD COLUMN IF NOT EXISTS avatar_seed     BIGINT,
    ADD COLUMN IF NOT EXISTS avatar_style    TEXT,
    -- Что реально применилось (может отличаться от planned_*: username мог
    -- быть занят и заменён вариантом). Отчёт обязан показывать факт, а не план.
    ADD COLUMN IF NOT EXISTS final_username  TEXT,
    ADD COLUMN IF NOT EXISTS avatar_applied  BOOLEAN NOT NULL DEFAULT FALSE;

-- Каталог объектов: фильтрация по проекту/роли/уровню без полного скана.
CREATE INDEX IF NOT EXISTS idx_gpt_plan_role
    ON global_presence_targets(plan_id, role);

CREATE INDEX IF NOT EXISTS idx_gpt_done_assets
    ON global_presence_targets(plan_id, asset_type)
    WHERE status = 'done';

-- Проверка уникальности username по уже созданным активам владельца:
-- аллокатор спрашивает «что занято» перед выдачей имени.
CREATE INDEX IF NOT EXISTS idx_gpt_final_username
    ON global_presence_targets(final_username)
    WHERE final_username IS NOT NULL;
