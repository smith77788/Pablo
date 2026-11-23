-- schema_v384: каналы-приёмники уведомлений о событиях ботов.
--
-- Раньше «запустили бота» и «заявка на вступление» уходили только владельцу
-- в личку. Владелец с несколькими сетями проектов хочет, чтобы события каждой
-- сети падали в СВОЙ канал: маршрут = куда (чат, где главный бот админ) +
-- какие боты (NULL — все) + какие события. Маршрутов у владельца сколько угодно,
-- одно событие уходит во все подходящие.
CREATE TABLE IF NOT EXISTS bot_notify_routes (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    chat_id     BIGINT NOT NULL,
    chat_title  TEXT NOT NULL DEFAULT '',
    bot_ids     BIGINT[],
    events      TEXT[] NOT NULL DEFAULT ARRAY['start','join_request']::TEXT[],
    mute_dm     BOOLEAN NOT NULL DEFAULT FALSE,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_notify_routes_owner ON bot_notify_routes(owner_id);
