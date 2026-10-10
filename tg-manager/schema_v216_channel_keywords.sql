-- v216: замер позиций каналов/чатов в поиске Telegram по ключам.
--
-- Трекинг позиций уже был, но только для БОТОВ (tracked_keywords.bot_id).
-- Каналы/чаты держатся в топе теми же ключами (имя + @username), но их позицию
-- никто не мерил. Здесь — отдельные таблицы под каналы/чаты: какие ключи
-- отслеживаем и какая позиция во времени (важен тренд: поднялись/просели).
--
-- Ключи заводятся автоматически Фабрикой при создании (имя ресурса = его запрос)
-- и вручную. Замер — операция check_channel_rankings (поиск глазами аккаунта).

CREATE TABLE IF NOT EXISTS channel_tracked_keywords (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT  NOT NULL,
    channel_id  BIGINT  NOT NULL,       -- managed_channels.channel_id
    keyword     TEXT    NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_chkw_owner ON channel_tracked_keywords(owner_id);
CREATE INDEX IF NOT EXISTS idx_chkw_channel ON channel_tracked_keywords(channel_id);

CREATE TABLE IF NOT EXISTS channel_search_rankings (
    id          BIGSERIAL PRIMARY KEY,
    keyword_id  BIGINT  NOT NULL REFERENCES channel_tracked_keywords(id) ON DELETE CASCADE,
    channel_id  BIGINT  NOT NULL,
    position    INTEGER,                -- NULL = не нашли в выдаче
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chrank_keyword ON channel_search_rankings(keyword_id, checked_at DESC);
