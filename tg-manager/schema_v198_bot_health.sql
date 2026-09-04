-- v198: здоровье управляемых ботов.
--
-- Опрос ботов (auto_responder) уже отличал отозванный токен от сетевой ошибки
-- и ставил такому боту экспоненциальный отступ, но знал об этом только
-- серверный лог. Снаружи бот оставался «активным»: подписчики ему писали, он
-- не отвечал, и владельцу об этом никто не сообщал. Молчит не внутренний
-- инструмент, а лицо, которым владелец повёрнут к своей аудитории.
--
-- Эти колонки дают боту память о последней ошибке, чтобы состояние было видно
-- на экране, а о поломке, которая сама не пройдёт, можно было сообщить один
-- раз (dead_notified_at — тот же приём, что у сторожа прокси).
ALTER TABLE managed_bots
    ADD COLUMN IF NOT EXISTS last_error       TEXT,
    ADD COLUMN IF NOT EXISTS last_error_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS fail_streak      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_ok_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dead_notified_at TIMESTAMPTZ;

-- Выборка «о чём ещё не сообщили» — по сломанным и необъявленным.
CREATE INDEX IF NOT EXISTS idx_bots_broken
    ON managed_bots(added_by) WHERE fail_streak > 0;
