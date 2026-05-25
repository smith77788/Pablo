-- Subscription plans and crypto payment records
CREATE TABLE IF NOT EXISTS subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL UNIQUE,
    plan        TEXT   NOT NULL CHECK (plan IN ('starter','pro','enterprise')),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);

CREATE TABLE IF NOT EXISTS payments (
    id              SERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    plan            TEXT NOT NULL,
    period_months   INTEGER NOT NULL DEFAULT 1,
    currency        TEXT NOT NULL CHECK (currency IN ('TON','USDT_TRC20')),
    amount_crypto   NUMERIC(18,6) NOT NULL,
    amount_usd      NUMERIC(10,2) NOT NULL,
    wallet_address  TEXT NOT NULL,
    tx_hash         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','confirming','confirmed','expired','failed')),
    reference       TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '30 minutes'),
    confirmed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_payments_user     ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status   ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_reference ON payments(reference);
