-- Platform-level referral system (v22)

CREATE TABLE IF NOT EXISTS platform_referral_codes (
    user_id      BIGINT PRIMARY KEY REFERENCES platform_users(user_id),
    code         VARCHAR(20) UNIQUE NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    total_clicks INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS platform_referrals (
    id                   BIGSERIAL PRIMARY KEY,
    referrer_id          BIGINT NOT NULL REFERENCES platform_users(user_id),
    referred_id          BIGINT NOT NULL REFERENCES platform_users(user_id),
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    activated_at         TIMESTAMPTZ,
    paid_at              TIMESTAMPTZ,
    welcome_bonus_given  BOOLEAN DEFAULT FALSE,
    UNIQUE(referrer_id, referred_id)
);
CREATE INDEX IF NOT EXISTS idx_plat_ref_referrer ON platform_referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_plat_ref_referred ON platform_referrals(referred_id);

CREATE TABLE IF NOT EXISTS referral_rewards (
    id       BIGSERIAL PRIMARY KEY,
    user_id  BIGINT NOT NULL REFERENCES platform_users(user_id),
    level    VARCHAR(20) NOT NULL,
    plan     VARCHAR(20) NOT NULL,
    days     INT NOT NULL,
    given_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, level)
);
