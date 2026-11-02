-- schema_v273: квота операций бесплатного тарифа и процент рефереру с оплат.
--
-- 1. free_op_usage — сколько операций владелец без подписки поставил в этом
--    месяце (services/free_quota.py). Доступ к операциям был бинарным: на
--    бесплатном тарифе нельзя было запустить ни одной, и после трёх дней
--    триала продукт для человека выключался целиком. Счётчик растёт атомарно
--    (INSERT .. ON CONFLICT .. WHERE used < limit), месяц — календарный UTC.
CREATE TABLE IF NOT EXISTS free_op_usage (
    owner_id   BIGINT NOT NULL,
    month      DATE   NOT NULL,
    used       INT    NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, month)
);

-- 2. referral_commissions — дни подписки, начисленные пригласившему за КАЖДУЮ
--    оплату приглашённого (services/referral_commission.py). Раньше награда
--    была только за пороги (5, 3, 10, 25 приглашённых) и заканчивалась на
--    последнем уровне. Один платёж — одно начисление: ключ (payment_ref).
CREATE TABLE IF NOT EXISTS referral_commissions (
    id           BIGSERIAL PRIMARY KEY,
    referrer_id  BIGINT NOT NULL,
    referred_id  BIGINT NOT NULL,
    payment_ref  TEXT   NOT NULL UNIQUE,
    paid_months  INT    NOT NULL,
    days         INT    NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_referral_commissions_referrer
    ON referral_commissions (referrer_id, created_at DESC);
