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

-- Ключ — keyword_id/bot_id, как во ВСЕЙ остальной подсистеме рейтинга
-- (tracked_keywords, search_rankings, search_change_events). `ranking_engine`
-- пытался жить на собственной модели «владелец + канал», которой в схеме нет
-- никогда не было, — из-за этого он не работал целиком, а не только здесь.
CREATE TABLE IF NOT EXISTS ranking_alerts (
    id            SERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    keyword_id    INTEGER REFERENCES tracked_keywords(id) ON DELETE CASCADE,
    bot_id        BIGINT,
    keyword       TEXT NOT NULL,      -- копия для показа: ключ могут удалить
    old_position  INTEGER,
    new_position  INTEGER,
    alert_type    TEXT NOT NULL,      -- improved | dropped | entered | lost
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    acknowledged  BOOLEAN DEFAULT FALSE
);

-- Экран просит «последние 50 оповещений владельца», часто — только непрочитанные.
CREATE INDEX IF NOT EXISTS idx_ranking_alerts_owner
    ON ranking_alerts (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ranking_alerts_pending
    ON ranking_alerts (owner_id)
    WHERE acknowledged = FALSE;

-- Регион выдачи выбирают в форме добавления ключа, а хранить его было негде:
-- экран показывал флаг, но всегда один и тот же.
ALTER TABLE tracked_keywords
    ADD COLUMN IF NOT EXISTS region TEXT NOT NULL DEFAULT 'ru';

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

-- 4) cf_worker_pool.fail_streak — дебаунс «воркер упал».
--    Код уже написан на эту колонку и даже несёт запасную ветку с комментарием
--    «колонка ещё не примигрировала». Миграции не появилось, поэтому запасная
--    ветка работала ВСЕГДА: разовый сетевой блип сразу переводил воркер в
--    'down', и аккаунты дёргались между воркерами каждый цикл — ровно то, от
--    чего дебаунс и должен был защищать (скачок exit-IP → AUTH_KEY_DUPLICATED).
ALTER TABLE cf_worker_pool
    ADD COLUMN IF NOT EXISTS fail_streak INTEGER NOT NULL DEFAULT 0;
