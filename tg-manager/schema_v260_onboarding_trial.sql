-- schema_v260: онбординг — первый результат новичка и пробный период.
--
-- Чеклист онбординга раньше гас по «в operation_queue есть строка», а первую
-- строку ставит сама платформа при подключении аккаунта (contacts_push). Теперь
-- активация = первая операция владельца, взявшая хотя бы одну цель, или
-- пройденный день прогрева (services/onboarding.py). Строка пишется один раз:
-- это и след для владельца/аналитики, и липкость — ветерану чеклист не
-- возвращается, когда его аккаунты позже умирают.

CREATE TABLE IF NOT EXISTS onboarding_progress (
    owner_id             BIGINT PRIMARY KEY,
    activated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_result_op_id   BIGINT,
    first_result_op_type TEXT,
    first_result_label   TEXT,
    first_result_ok      INT
);

-- Пробный период (services/trial.py): один раз на владельца. account_tg_user_ids —
-- Telegram-аккаунты, с которыми он получен: второй бот-пользователь с теми же
-- аккаунтами второй триал не получает. Подписка триала — обычная строка
-- subscriptions (plan='paid'), эта таблица — защита от повтора и след выдачи.
CREATE TABLE IF NOT EXISTS subscription_trials (
    owner_id            BIGINT PRIMARY KEY,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    days                INT NOT NULL,
    account_tg_user_ids BIGINT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_subscription_trials_tg_ids
    ON subscription_trials USING GIN (account_tg_user_ids);
