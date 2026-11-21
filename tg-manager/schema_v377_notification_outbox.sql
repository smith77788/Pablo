-- schema_v377: очередь недоставленных уведомлений владельцу.
--
-- Симптом из прода (21.11.2026): тревога «отчёт об операции не доехал до
-- владельца — 21 раз(а)». Уведомление пробовали отправить три раза за пять
-- секунд, и на сетевом сбое или лимите Telegram дольше этого отчёт пропадал
-- навсегда: отметку анти-повтора снимали, но повторять было некому — итог
-- операции событие однократное.
--
-- Теперь недоставленное по временной причине ложится сюда, и фоновый цикл
-- (services/notify_outbox.py) повторяет его с нарастающей паузой до суток.
-- Сама операция при этом не ждёт: её итог уже записан, а отчёт догоняет.
CREATE TABLE IF NOT EXISTS notification_outbox (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT      NOT NULL,
    pref            TEXT        NOT NULL DEFAULT '',
    text            TEXT        NOT NULL,
    reply_markup    TEXT,
    attempts        INT         NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox (next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_user
    ON notification_outbox (user_id);
