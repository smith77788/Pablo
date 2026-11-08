-- schema_v311: когда новичок закрыл каждый шаг онбординга (services/onboarding.py).
--
-- В документе-плане стояло: «мерить время до первого результата и долю
-- дошедших по шагам». Момент первого результата писался (onboarding_progress),
-- но начала пути и промежуточных шагов не было, поэтому ни время, ни воронку
-- посчитать было не из чего. Строка — первый раз, когда шаг увидели закрытым;
-- шаг 'start' — первое открытие чеклиста, от него и считается время.
CREATE TABLE IF NOT EXISTS onboarding_step_log (
    owner_id BIGINT      NOT NULL,
    step_id  TEXT        NOT NULL,
    goal     TEXT,
    done_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_onboarding_step_log_start
    ON onboarding_step_log (done_at) WHERE step_id = 'start';
