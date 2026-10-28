-- v210: «Нотариус» — независимое заверение рекламных размещений.
--
-- Рынок рекламы в Telegram держится на честном слове: покупатель не может
-- доказать ни что пост висел обещанный срок, ни что его вообще публиковали.
-- Внешние счётчики смотрят снаружи и ОЦЕНИВАЮТ; панель наблюдателей сидит
-- внутри канала и составляет ПРОТОКОЛ — с хронологией и подписью.
--
-- Наблюдение — пассивное чтение: банориска почти нет, аккаунт от него не
-- изнашивается. Это переворачивает роль флота: он перестаёт быть расходником
-- и становится измерительным прибором.

-- Наблюдение за одним размещением.
CREATE TABLE IF NOT EXISTS notary_watches (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    channel_ref    TEXT NOT NULL,              -- @username или -100…
    channel_title  TEXT,
    -- id поста. NULL — пост ещё не вышел: наблюдаем ПОЯВЛЕНИЕ, чтобы уметь
    -- доказывать и обратное («оплачено, не опубликовано»).
    msg_id         BIGINT,
    advertiser     TEXT,                       -- кому продан слот (со слов продавца)
    price_paid     NUMERIC(12,2),
    currency       TEXT DEFAULT 'USD',
    -- Обещанное окно удержания. Всё, что сравнивается с фактом, берётся отсюда.
    promised_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    promised_until TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',   -- active|done|cancelled
    verdict        TEXT,                             -- см. services/notary.py
    -- Факты, сведённые из наблюдений (денормализация ради списка без JOIN).
    first_seen_at  TIMESTAMPTZ,
    last_seen_at   TIMESTAMPTZ,
    absent_since   TIMESTAMPTZ,                -- первое ПОДТВЕРЖДЁННОЕ отсутствие
    views_first    INTEGER,
    views_last     INTEGER,
    checks_done    INTEGER NOT NULL DEFAULT 0,
    next_check_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Подпись итогового протокола (HMAC, тот же секрет, что у compliance).
    cert_sig       TEXT,
    cert_issued_at TIMESTAMPTZ,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notary_watches_owner
    ON notary_watches(owner_id, created_at DESC);
-- Очередь фонового наблюдателя: «кого проверять прямо сейчас».
CREATE INDEX IF NOT EXISTS idx_notary_watches_due
    ON notary_watches(next_check_at) WHERE status = 'active';

-- Журнал наблюдений. Каждая строка — одно заглядывание в канал.
-- Строки НЕ обновляются и не удаляются: протокол теряет смысл, если его можно
-- переписать задним числом.
CREATE TABLE IF NOT EXISTS notary_observations (
    id          BIGSERIAL PRIMARY KEY,
    watch_id    BIGINT NOT NULL REFERENCES notary_watches(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- present | absent | unknown. `unknown` — наблюдение НЕ состоялось (сеть,
    -- флуд, прокси). Оно никогда не читается как снятие поста: обвинить канал
    -- из-за собственной сетевой ошибки — худшее, что может сделать нотариус.
    state       TEXT NOT NULL,
    views       INTEGER,
    acc_id      BIGINT,                        -- каким наблюдателем смотрели
    geo         TEXT,                          -- гео прокси наблюдателя
    detail      TEXT,                          -- причина unknown
    sig         TEXT                           -- HMAC наблюдения
);
CREATE INDEX IF NOT EXISTS idx_notary_obs_watch
    ON notary_observations(watch_id, observed_at);
