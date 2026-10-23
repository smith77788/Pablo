-- Защита от накрутки/ботов (per-bot, ВЫКЛючена по умолчанию).
-- ЦА платформы часто использует накрутку намеренно (SEO-выдача), поэтому защита
-- включается отдельно на каждого бота и срабатывает на РЕЗКИЙ всплеск активности.
-- Изоляция подозрительных — структурная, в единственной точке (аудитория рассылок),
-- флаг suspect выставляется ТОЛЬКО когда защита включена.

-- Конфиг защиты по боту. Нет строки = защита off (ничего не меняется).
CREATE TABLE IF NOT EXISTS bot_flood_config (
    bot_id            BIGINT PRIMARY KEY REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    owner_id          BIGINT NOT NULL,
    mode              TEXT   NOT NULL DEFAULT 'off',  -- off|detect|protect|block
    threshold_per_min INT    NOT NULL DEFAULT 30,     -- новых подписчиков/мин → всплеск
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Эпизоды атак — для статуса, аналитики и «очистки волны».
CREATE TABLE IF NOT EXISTS bot_flood_events (
    id              BIGSERIAL PRIMARY KEY,
    bot_id          BIGINT NOT NULL,
    owner_id        BIGINT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    peak_per_min    INT NOT NULL DEFAULT 0,
    suspected_count INT NOT NULL DEFAULT 0,
    mode            TEXT NOT NULL DEFAULT '',   -- режим, действовавший во время эпизода
    status          TEXT NOT NULL DEFAULT 'active'  -- active|ended
);
CREATE INDEX IF NOT EXISTS idx_bot_flood_events_bot ON bot_flood_events(bot_id, status);

-- Пометка подозрительного подписчика (накрутка). Исключается из аудитории рассылок.
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS suspect BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS flagged_at TIMESTAMPTZ;
-- Частичный индекс — быстрые выборки/очистка только по помеченным.
CREATE INDEX IF NOT EXISTS idx_bot_users_suspect ON bot_users(bot_id) WHERE suspect;
