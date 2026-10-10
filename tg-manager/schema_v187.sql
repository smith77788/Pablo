-- Состояние безопасного инвайтинга по КОНКРЕТНОМУ чату (не по аккаунту).
-- Зачем: Telegram оценивает не только аккаунт-инвайтера, но и сам чат — частоту
-- системных событий о вступлении, соотношение живой активности к «холодному»
-- притоку, ранние выходы и жалобы. При аномалии он «замораживает» ПРИЁМ в чат
-- (chat-level FLOOD_WAIT), и следующий инвайт ломается независимо от аккаунта.
-- Поэтому темп, паузы и негативные сигналы нужно копить ПО ЧАТУ и переживать
-- рестарт/смену исполняющего аккаунта. См. services/smart_invite.py.
CREATE TABLE IF NOT EXISTS chat_invite_state (
    chat_key        TEXT NOT NULL,          -- нормализованная ссылка/username/id чата
    owner_id        BIGINT NOT NULL,
    -- Скользящее окно частоты (сброс governor'ом по времени)
    window_start    TIMESTAMPTZ,
    window_count    INTEGER NOT NULL DEFAULT 0,
    -- Заморозка приёма в чат (chat-flood / серия негатива)
    paused_until    TIMESTAMPTZ,
    pause_reason    TEXT,
    -- Кумулятивные исходы — для соотношения «прижилось / отвалилось»
    invited_total   INTEGER NOT NULL DEFAULT 0,
    joined_total    INTEGER NOT NULL DEFAULT 0,   -- подтверждённые вступления
    left_total      INTEGER NOT NULL DEFAULT 0,   -- вышли вскоре
    reported_total  INTEGER NOT NULL DEFAULT 0,   -- пожаловались
    flood_hits      INTEGER NOT NULL DEFAULT 0,   -- сколько раз чат отвечал flood
    -- «Живость» чата (участники/свежесть активности), 0..1
    liveness_score      DOUBLE PRECISION,
    liveness_checked_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, chat_key)
);
