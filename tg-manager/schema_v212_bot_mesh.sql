-- v212: Bot Mesh — координация задач между ботами (bot-to-bot).
--
-- Telegram РЕАЛЬНО поддерживает bot-to-bot (opt-in в @BotFather). Но гашение
-- петель Telegram НЕ делает — «bot-message handling must terminate predictably»
-- лежит на нас. Отсюда envelope с задачей, глубиной, дедлайном и трассой: по
-- ним ядро решает обрабатывать шаг или отбросить (петля/дубль/просрочка).
--
-- Владелец видит цепочку Sales→Qualification→CRM как один workflow; на деле это
-- задача, которую боты передают по маршруту, а мы следим, чтобы она завершилась.

-- Одна задача, идущая по цепочке ботов.
CREATE TABLE IF NOT EXISTS bot_mesh_tasks (
    task_id      TEXT   PRIMARY KEY,          -- uuid, стабилен на весь путь
    owner_id     BIGINT NOT NULL,
    origin_bot   BIGINT,                      -- кто запустил цепочку
    route        JSONB  NOT NULL DEFAULT '[]',-- упорядоченные шаги [{bot,capability}]
    step         INTEGER NOT NULL DEFAULT 0,  -- текущий шаг в маршруте
    depth        INTEGER NOT NULL DEFAULT 0,  -- сколько передач уже было (антипетля)
    trace        JSONB  NOT NULL DEFAULT '[]',-- список bot_id по порядку прохождения
    payload      JSONB  NOT NULL DEFAULT '{}',
    status       TEXT   NOT NULL DEFAULT 'running',  -- running|done|dropped|failed
    drop_reason  TEXT,                         -- почему отброшена (expired|max_depth|…)
    deadline_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_mesh_owner
    ON bot_mesh_tasks(owner_id, created_at DESC);

-- Трасса передач. Только дописывается: по ней видно весь путь задачи и на ней
-- же строится дедуп (один и тот же шаг дважды = петля).
CREATE TABLE IF NOT EXISTS bot_mesh_hops (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT   NOT NULL REFERENCES bot_mesh_tasks(task_id) ON DELETE CASCADE,
    step        INTEGER NOT NULL,
    from_bot    BIGINT,
    to_bot      BIGINT,
    capability  TEXT,
    outcome     TEXT,                          -- accepted|dropped|handled|failed
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_mesh_hops_task
    ON bot_mesh_hops(task_id, created_at);
-- Дедуп шага: (задача, шаг, кому) — уникально. Повтор = петля, вставка упадёт.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_mesh_hop_step
    ON bot_mesh_hops(task_id, step, to_bot);
