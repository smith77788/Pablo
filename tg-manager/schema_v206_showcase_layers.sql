-- v206: витрина — буфер между дочерней группой и боевым каналом.
--
-- «Мать-Дочка» закрывает бан за сам инвайт: сгорает расходная дочерняя группа,
-- а не боевой канал. Но она открывает второй вектор — приглашённые идут в мать
-- по одной закреплённой ссылке, и канал получает двести вступлений за два часа.
-- Это и есть сигнал, по которому канал закрывают.
--
-- Витрина копит аудиторию, а в мать пускает ссылками С ЛИМИТОМ ВСТУПЛЕНИЙ,
-- выпускаемыми волнами: пропускную способность задаёт система, а не скорость
-- кликов. Логика волн — services/showcase_layer.py (чистая, без сети и базы).
CREATE TABLE IF NOT EXISTS showcase_layers (
    id                 BIGSERIAL PRIMARY KEY,
    owner_id           BIGINT NOT NULL,
    -- Боевой канал, ради которого всё и делается. Ключ пары вместе с owner_id.
    mother_ref         TEXT NOT NULL,
    -- Сама витрина: канал, куда ведёт дочерняя группа.
    channel_id         BIGINT NOT NULL,
    access_hash        BIGINT NOT NULL DEFAULT 0,
    showcase_ref       TEXT NOT NULL,          -- invite-ссылка на витрину
    creator_account_id BIGINT,
    status             TEXT NOT NULL DEFAULT 'active',   -- active|burned
    -- Темп: когда ушла последняя волна и сколько всего выпущено.
    last_wave_at       TIMESTAMPTZ,
    waves_released     INTEGER NOT NULL DEFAULT 0,
    -- Действующая ссылка в мать и её лимит — чтобы не выпускать вторую, пока
    -- первая не исчерпана.
    active_link        TEXT,
    active_link_limit  INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    burned_at          TIMESTAMPTZ,
    burn_reason        TEXT
);

-- Поиск активной витрины пары (владелец, мать) — то, что делается на каждом
-- запуске инвайта.
CREATE INDEX IF NOT EXISTS idx_showcase_owner_mother
    ON showcase_layers(owner_id, mother_ref, status);

-- Выборка «кому пора выпускать волну» для фонового цикла.
CREATE INDEX IF NOT EXISTS idx_showcase_due
    ON showcase_layers(last_wave_at) WHERE status = 'active';
