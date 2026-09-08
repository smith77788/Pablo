-- v200: боты, найденные на аккаунтах флота (скан через @BotFather /mybots).
--
-- Разрыв: скан флота находил каналы и чаты, но БОТОВ не находил никогда — бот,
-- созданный аккаунтом, это не Channel и в обходе диалогов как «свой ресурс» не
-- виден. На живом флоте это давало «164 канала/чата, 32 аккаунта и 0 ботов»,
-- хотя боты есть, а подключить их можно было только по одному, вручную вставляя
-- токен от @BotFather.
--
-- Почему отдельная таблица, а не managed_bots: у найденного бота есть username и
-- аккаунт-владелец, но НЕТ токена (managed_bots.token — UNIQUE NOT NULL, там
-- лежат подключённые боты). Найденный ≠ подключённый, и смешивать их значило бы
-- показывать как управляемое то, чем управлять пока нельзя.
CREATE TABLE IF NOT EXISTS discovered_bots (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    acc_id        BIGINT,                 -- аккаунт-владелец бота (чей BotFather)
    username      TEXT   NOT NULL,        -- без «@»
    -- Когда бота подключат по токену, сюда ложится его bot_id из managed_bots —
    -- чтобы список найденных честно показывал, что уже подключено.
    linked_bot_id BIGINT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, username)
);
CREATE INDEX IF NOT EXISTS idx_discovered_bots_owner
    ON discovered_bots(owner_id, last_seen DESC);
