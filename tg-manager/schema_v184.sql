-- Ещё две таблицы, которых не заводила ни одна миграция.
--
-- Найдено тем же способом, что и «Воркфлоу»: сверкой таблиц, упомянутых в SQL
-- кода, с теми, что реально есть в базе после полного накатывания схемы.
--
-- 1) ranking_alerts — оповещения о смене позиции в поиске.
--    Создавала её только `ranking_engine.init_ranking_tables`, которую никто не
--    вызывает. Соседние таблицы (search_rankings, tracked_keywords) в схеме
--    есть, поэтому раздел выглядел живым, но:
--      • оповещения не записывались вовсе (запись падала и проглатывалась);
--      • /api/miniapp/ranking/alerts всегда отдавал пустой список;
--      • get_ranking_stats считает всё в ОДНОМ try — падение на подсчёте
--        оповещений роняло весь блок статистики, и панель показывала пусто.
--
-- 2) strike_appeals — поданные апелляции на блокировку.
--    Создателя не было нигде. Письма в Telegram при этом реально уходили, а
--    запись о поданной апелляции терялась молча (log.debug), и статус узнать
--    было невозможно — `get_appeal_status` всегда отвечал ошибкой.

CREATE TABLE IF NOT EXISTS ranking_alerts (
    id            SERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    channel_id    BIGINT NOT NULL,
    keyword       TEXT NOT NULL,
    old_position  INTEGER,
    new_position  INTEGER,
    alert_type    TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    acknowledged  BOOLEAN DEFAULT FALSE
);

-- Экран просит «последние 50 оповещений владельца», часто — только непрочитанные.
CREATE INDEX IF NOT EXISTS idx_ranking_alerts_owner
    ON ranking_alerts (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ranking_alerts_pending
    ON ranking_alerts (owner_id)
    WHERE acknowledged = FALSE;

CREATE TABLE IF NOT EXISTS strike_appeals (
    -- id задаёт код: 'APPEAL-<account_id>-<UTC-время>' — человекочитаемый номер
    -- обращения, который показывают пользователю. Поэтому TEXT, а не SERIAL.
    id          TEXT PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    account_id  BIGINT,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'submitted',
    emails_sent INTEGER NOT NULL DEFAULT 0,
    details     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- «Мои апелляции» — единственный список, который отсюда читают.
CREATE INDEX IF NOT EXISTS idx_strike_appeals_owner
    ON strike_appeals (owner_id, created_at DESC);

-- 3) notification_dedup — персистентный анти-спам повторяющихся уведомлений.
--    Таблицу заводил сам `notify_dedup_ok` — «CREATE TABLE IF NOT EXISTS» на
--    КАЖДОМ уведомлении. Работало, но это DDL в горячем пути (лишний рейс до
--    базы и блокировка на каждое сообщение), и ровно тот приём, из-за которого
--    умер раздел «Воркфлоу»: создатель, которого перестают вызывать, не
--    оставляет следа. Место таблицы — в миграции.
CREATE TABLE IF NOT EXISTS notification_dedup (
    user_id   BIGINT      NOT NULL,
    dedup_key TEXT        NOT NULL,
    last_sent TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, dedup_key)
);

-- Чистка старых записей идёт по времени.
CREATE INDEX IF NOT EXISTS idx_notification_dedup_sent
    ON notification_dedup (last_sent);
