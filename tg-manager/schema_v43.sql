-- schema_v43: Referral conversions + funnel completion tracking
CREATE TABLE IF NOT EXISTS referral_conversions (
    id              BIGSERIAL PRIMARY KEY,
    bot_id          BIGINT NOT NULL,
    referrer_id     BIGINT NOT NULL,
    referred_id     BIGINT NOT NULL,
    conversion_type TEXT NOT NULL DEFAULT 'funnel_complete',  -- funnel_complete|relay_reply|purchase
    funnel_id       INT,
    meta            JSONB DEFAULT '{}',
    occurred_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ref_conv_referrer ON referral_conversions(referrer_id, bot_id);
CREATE INDEX IF NOT EXISTS idx_ref_conv_occurred ON referral_conversions(occurred_at);

-- Track funnel completion
ALTER TABLE funnel_subscriptions
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS conversion_recorded BOOLEAN DEFAULT false;
