-- Nevesty Models — Missing Features Migration
-- Apply this to your existing Lovable/Supabase project.
-- Prerequisites: models, bookings, app_settings tables must already exist.
-- Run in Supabase SQL Editor.

-- ── reviews ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
  id BIGSERIAL PRIMARY KEY,
  model_id BIGINT REFERENCES models(id) ON DELETE SET NULL,
  booking_id BIGINT REFERENCES bookings(id) ON DELETE SET NULL,
  client_chat_id TEXT,
  client_name TEXT,
  rating INTEGER CHECK (rating BETWEEN 1 AND 5),
  text TEXT,
  approved BOOLEAN DEFAULT FALSE,
  status TEXT DEFAULT 'pending',
  admin_reply TEXT,
  admin_reply_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;
CREATE POLICY "reviews_public_read" ON reviews FOR SELECT USING (approved = TRUE);
CREATE POLICY "reviews_insert_all" ON reviews FOR INSERT WITH CHECK (TRUE);
CREATE POLICY "reviews_admin_all" ON reviews FOR ALL USING (auth.role() = 'authenticated');
CREATE INDEX IF NOT EXISTS idx_reviews_model ON reviews(model_id);
CREATE INDEX IF NOT EXISTS idx_reviews_approved ON reviews(approved);

-- ── promo_codes ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promo_codes (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  discount_type TEXT DEFAULT 'percent' CHECK (discount_type IN ('percent','fixed')),
  discount_value NUMERIC NOT NULL,
  min_budget NUMERIC DEFAULT 0,
  max_uses INTEGER DEFAULT NULL,
  used_count INTEGER DEFAULT 0,
  valid_from TIMESTAMPTZ DEFAULT NOW(),
  valid_until TIMESTAMPTZ DEFAULT NULL,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE promo_codes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "promo_admin_all" ON promo_codes FOR ALL USING (auth.role() = 'authenticated');
-- Public check by code (for client-side validation):
CREATE POLICY "promo_public_validate" ON promo_codes FOR SELECT USING (active = TRUE AND (valid_until IS NULL OR valid_until > NOW()));

-- ── loyalty_points ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loyalty_points (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT NOT NULL,
  points INTEGER DEFAULT 0,
  total_earned INTEGER DEFAULT 0,
  level TEXT DEFAULT 'bronze',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_loyalty_chat ON loyalty_points(chat_id);
ALTER TABLE loyalty_points ENABLE ROW LEVEL SECURITY;
CREATE POLICY "loyalty_admin_all" ON loyalty_points FOR ALL USING (auth.role() = 'authenticated');

-- ── loyalty_transactions ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loyalty_transactions (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT NOT NULL,
  points INTEGER NOT NULL,
  type TEXT NOT NULL,
  description TEXT,
  booking_id BIGINT REFERENCES bookings(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_loyalty_tx_chat ON loyalty_transactions(chat_id);
ALTER TABLE loyalty_transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "loyalty_tx_admin" ON loyalty_transactions FOR ALL USING (auth.role() = 'authenticated');

-- ── referrals ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
  id BIGSERIAL PRIMARY KEY,
  referrer_chat_id TEXT NOT NULL,
  referred_chat_id TEXT NOT NULL UNIQUE,
  bonus_points INTEGER DEFAULT 50,
  activated BOOLEAN DEFAULT FALSE,
  activated_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;
CREATE POLICY "referrals_admin" ON referrals FOR ALL USING (auth.role() = 'authenticated');

-- ── scheduled_broadcasts ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  photo_url TEXT,
  target TEXT DEFAULT 'all' CHECK (target IN ('all','clients','new_clients','city')),
  target_city TEXT,
  scheduled_at TIMESTAMPTZ NOT NULL,
  sent_at TIMESTAMPTZ,
  status TEXT DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','cancelled')),
  sent_count INTEGER DEFAULT 0,
  error_count INTEGER DEFAULT 0,
  created_by TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE scheduled_broadcasts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "broadcasts_admin" ON scheduled_broadcasts FOR ALL USING (auth.role() = 'authenticated');
CREATE INDEX IF NOT EXISTS idx_broadcasts_status ON scheduled_broadcasts(status, scheduled_at);

-- ── ab_experiments ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ab_experiments (
  id TEXT PRIMARY KEY,
  hypothesis TEXT NOT NULL,
  metric TEXT NOT NULL,
  effort TEXT DEFAULT 'low',
  expected_lift TEXT,
  status TEXT DEFAULT 'proposed' CHECK (status IN ('proposed','running','completed','archived')),
  department TEXT DEFAULT 'CEO',
  result TEXT,
  result_data JSONB,
  started_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE ab_experiments ENABLE ROW LEVEL SECURITY;
CREATE POLICY "experiments_admin" ON ab_experiments FOR ALL USING (auth.role() = 'authenticated');

-- ── notifications ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT,
  message TEXT NOT NULL,
  data JSONB,
  read BOOLEAN DEFAULT FALSE,
  sent BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_chat ON notifications(chat_id, read);
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
CREATE POLICY "notifications_admin" ON notifications FOR ALL USING (auth.role() = 'authenticated');

-- ── agent_logs ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_logs (
  id BIGSERIAL PRIMARY KEY,
  agent_role TEXT NOT NULL,
  department TEXT NOT NULL,
  cycle_id TEXT,
  status TEXT DEFAULT 'running',
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  duration_ms INTEGER DEFAULT 0,
  result_summary TEXT,
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_dept ON agent_logs(department, created_at DESC);
ALTER TABLE agent_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "agent_logs_admin" ON agent_logs FOR ALL USING (auth.role() = 'authenticated');

-- ── agent_discussions ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_discussions (
  id BIGSERIAL PRIMARY KEY,
  cycle_id TEXT,
  department TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discussions_cycle ON agent_discussions(cycle_id);
ALTER TABLE agent_discussions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "discussions_admin" ON agent_discussions FOR ALL USING (auth.role() = 'authenticated');

-- ── faq ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS faq (
  id BIGSERIAL PRIMARY KEY,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  category TEXT DEFAULT 'general',
  sort_order INTEGER DEFAULT 0,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE faq ENABLE ROW LEVEL SECURITY;
CREATE POLICY "faq_public_read" ON faq FOR SELECT USING (active = TRUE);
CREATE POLICY "faq_admin_all" ON faq FOR ALL USING (auth.role() = 'authenticated');

-- ── price_packages ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_packages (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  duration_hours INTEGER NOT NULL,
  base_price NUMERIC NOT NULL,
  category TEXT,
  includes TEXT,
  sort_order INTEGER DEFAULT 0,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE price_packages ENABLE ROW LEVEL SECURITY;
CREATE POLICY "packages_public_read" ON price_packages FOR SELECT USING (active = TRUE);
CREATE POLICY "packages_admin_all" ON price_packages FOR ALL USING (auth.role() = 'authenticated');

-- ── error_logs ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS error_logs (
  id BIGSERIAL PRIMARY KEY,
  error_type TEXT NOT NULL,
  message TEXT NOT NULL,
  stack TEXT,
  context JSONB,
  resolved BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE error_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "error_logs_admin" ON error_logs FOR ALL USING (auth.role() = 'authenticated');

-- ── Extend bookings table with promo/payment columns (if not already present) ──
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bookings' AND column_name='promo_code') THEN
    ALTER TABLE bookings ADD COLUMN promo_code TEXT;
    ALTER TABLE bookings ADD COLUMN promo_discount NUMERIC DEFAULT 0;
    ALTER TABLE bookings ADD COLUMN payment_status TEXT DEFAULT 'unpaid' CHECK (payment_status IN ('unpaid','pending','paid','refunded'));
    ALTER TABLE bookings ADD COLUMN payment_id TEXT;
    ALTER TABLE bookings ADD COLUMN payment_provider TEXT;
    ALTER TABLE bookings ADD COLUMN paid_at TIMESTAMPTZ;
  END IF;
END $$;

-- ── Additional app_settings seed data ────────────────────────────────────────
-- Only inserts if key doesn't already exist
INSERT INTO app_settings (key, value) VALUES
  ('loyalty_enabled', '0'),
  ('referral_enabled', '0'),
  ('reviews_auto_approve', '0'),
  ('reviews_min_completed', '1'),
  ('booking_auto_confirm', '0'),
  ('booking_require_email', '0'),
  ('notif_new_order', '1'),
  ('notif_new_review', '1'),
  ('notif_new_message', '1'),
  ('faq_enabled', '1'),
  ('promo_enabled', '1')
ON CONFLICT (key) DO NOTHING;
