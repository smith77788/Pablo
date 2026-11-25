-- schema_v385: лента уведомлений владельца внутри мини-аппа.
--
-- Уведомления существовали только как сообщения в личке бота: «операция
-- завершилась», «аккаунт ограничен», «новый подписчик». Владелец, который был
-- не в сети или промотал чат, не мог посмотреть, что произошло, — истории
-- не было нигде. Здесь она есть: каждое уведомление, прошедшее настройки
-- владельца, попадает в ленту (одна дверь — db.notify_if_enabled), и мини-апп
-- показывает её списком с отметкой непрочитанного.
--
-- read_at — когда владелец увидел строку в приложении. Непрочитанные считает
-- значок на экране «Итоги».
CREATE TABLE IF NOT EXISTS notification_feed (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    pref       TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    dedup_key  TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at    TIMESTAMPTZ
);
-- Лента читается страницами по владельцу и по непрочитанным: один индекс
-- закрывает оба запроса (владелец + время), второй — счётчик непрочитанных.
CREATE INDEX IF NOT EXISTS idx_notification_feed_owner
    ON notification_feed(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_feed_unread
    ON notification_feed(owner_id) WHERE read_at IS NULL;
