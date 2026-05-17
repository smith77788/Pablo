-- Nevesty Models — Initial Supabase Schema
-- Converted from SQLite (database.js)
-- Compatible with Supabase (PostgreSQL 15+)

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── schema_versions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_versions (
  version INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── admins ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
  id BIGSERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  email TEXT,
  password_hash TEXT NOT NULL,
  telegram_id TEXT,
  role TEXT DEFAULT 'manager',
  totp_secret TEXT DEFAULT NULL,
  totp_enabled BOOLEAN DEFAULT FALSE,
  last_login TIMESTAMPTZ DEFAULT NULL,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── models ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS models (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  age INTEGER,
  height INTEGER,
  weight INTEGER,
  bust INTEGER,
  waist INTEGER,
  hips INTEGER,
  shoe_size TEXT,
  hair_color TEXT,
  eye_color TEXT,
  bio TEXT,
  photo_main TEXT,
  photos TEXT DEFAULT '[]',
  instagram TEXT,
  category TEXT DEFAULT 'fashion',
  available BOOLEAN DEFAULT TRUE,
  city TEXT,
  featured BOOLEAN DEFAULT FALSE,
  phone TEXT,
  order_count INTEGER DEFAULT 0,
  view_count INTEGER DEFAULT 0,
  archived BOOLEAN DEFAULT FALSE,
  video_url TEXT DEFAULT NULL,
  telegram_chat_id TEXT DEFAULT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE models ENABLE ROW LEVEL SECURITY;
CREATE POLICY "models_public_read" ON models FOR SELECT USING (archived = FALSE AND available = TRUE);
CREATE POLICY "models_admin_all" ON models FOR ALL USING (auth.role() = 'authenticated');

-- ── orders ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  order_number TEXT UNIQUE NOT NULL,
  client_name TEXT NOT NULL,
  client_phone TEXT NOT NULL,
  client_email TEXT,
  client_telegram TEXT,
  client_chat_id TEXT,
  model_id BIGINT REFERENCES models(id),
  event_type TEXT NOT NULL,
  event_date TEXT,
  event_duration INTEGER DEFAULT 4,
  location TEXT,
  budget TEXT,
  comments TEXT,
  status TEXT DEFAULT 'new',
  admin_notes TEXT,
  manager_id BIGINT REFERENCES admins(id),
  review_requested TIMESTAMPTZ DEFAULT NULL,
  model_ids TEXT DEFAULT NULL,
  reminder_sent_at TEXT DEFAULT NULL,
  utm_source TEXT DEFAULT '',
  utm_medium TEXT DEFAULT '',
  utm_campaign TEXT DEFAULT '',
  payment_id TEXT DEFAULT NULL,
  payment_status TEXT DEFAULT NULL,
  paid_at TIMESTAMPTZ DEFAULT NULL,
  payment_url TEXT DEFAULT NULL,
  payment_amount INTEGER DEFAULT NULL,
  invoice_sent_at TEXT DEFAULT NULL,
  internal_note TEXT,
  completed_at TIMESTAMPTZ DEFAULT NULL,
  cancelled_at TIMESTAMPTZ DEFAULT NULL,
  reminded_at TIMESTAMPTZ DEFAULT NULL,
  review_invitation_sent_at TIMESTAMPTZ DEFAULT NULL,
  reminder_24h_sent TIMESTAMPTZ DEFAULT NULL,
  event_time TEXT DEFAULT NULL,
  completed_reminder_sent TIMESTAMPTZ DEFAULT NULL,
  deposit_amount INTEGER DEFAULT NULL,
  stars_payment_charge_id TEXT DEFAULT NULL,
  promo_code_id BIGINT DEFAULT NULL,
  discount_amount NUMERIC DEFAULT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY "orders_admin_all" ON orders FOR ALL USING (auth.role() = 'authenticated');

-- ── messages ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id),
  sender_type TEXT NOT NULL,
  sender_name TEXT,
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── telegram_sessions ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS telegram_sessions (
  chat_id TEXT PRIMARY KEY,
  state TEXT DEFAULT 'idle',
  order_id BIGINT,
  data TEXT DEFAULT '{}',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── agent_logs ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_logs (
  id BIGSERIAL PRIMARY KEY,
  from_name TEXT,
  message TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── agent_findings ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_findings (
  id BIGSERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  file TEXT,
  line INTEGER,
  auto_fixable BOOLEAN DEFAULT FALSE,
  proposed_fix TEXT,
  status TEXT DEFAULT 'open',
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  fixed_by TEXT,
  fix_summary TEXT,
  fixed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── agent_discussions ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_discussions (
  id BIGSERIAL PRIMARY KEY,
  from_agent TEXT NOT NULL,
  to_agent TEXT DEFAULT 'all',
  topic TEXT NOT NULL,
  message TEXT NOT NULL,
  ref_finding_id BIGINT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── bot_settings ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bot_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY "bot_settings_admin_all" ON bot_settings FOR ALL USING (auth.role() = 'authenticated');
CREATE POLICY "bot_settings_public_read" ON bot_settings FOR SELECT USING (TRUE);

-- ── reviews ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
  id BIGSERIAL PRIMARY KEY,
  client_name TEXT NOT NULL,
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  text TEXT NOT NULL,
  model_id BIGINT,
  approved BOOLEAN DEFAULT FALSE,
  status TEXT DEFAULT 'pending',
  order_id BIGINT DEFAULT NULL,
  chat_id TEXT DEFAULT NULL,
  admin_reply TEXT,
  reply_at TEXT,
  rejected BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;
CREATE POLICY "reviews_public_read" ON reviews FOR SELECT USING (approved = TRUE AND rejected = FALSE);
CREATE POLICY "reviews_admin_all" ON reviews FOR ALL USING (auth.role() = 'authenticated');

-- ── order_notes ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_notes (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id),
  admin_note TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── order_status_history ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_status_history (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id),
  old_status TEXT,
  new_status TEXT NOT NULL,
  changed_by TEXT,
  notes TEXT DEFAULT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── factory_tasks ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS factory_tasks (
  id BIGSERIAL PRIMARY KEY,
  action TEXT NOT NULL,
  priority INTEGER DEFAULT 5,
  department TEXT,
  expected_impact TEXT,
  status TEXT DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── loyalty_points ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loyalty_points (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL UNIQUE,
  points INTEGER DEFAULT 0,
  total_earned INTEGER DEFAULT 0,
  level TEXT DEFAULT 'bronze',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── loyalty_transactions ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loyalty_transactions (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  points INTEGER NOT NULL,
  type TEXT NOT NULL,
  description TEXT,
  order_id BIGINT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── referrals ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
  id BIGSERIAL PRIMARY KEY,
  referrer_chat_id BIGINT NOT NULL,
  referred_chat_id BIGINT NOT NULL,
  bonus_points INTEGER DEFAULT 50,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── achievements ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS achievements (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  achievement_key TEXT NOT NULL,
  achieved_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (chat_id, achievement_key)
);

-- ── blocked_clients ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS blocked_clients (
  chat_id BIGINT PRIMARY KEY,
  reason TEXT,
  blocked_at TIMESTAMPTZ DEFAULT NOW(),
  blocked_by BIGINT
);

-- ── client_prefs ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS client_prefs (
  chat_id BIGINT PRIMARY KEY,
  notify_status BOOLEAN DEFAULT TRUE,
  notify_promo BOOLEAN DEFAULT TRUE,
  notify_review BOOLEAN DEFAULT TRUE,
  notify_reminders BOOLEAN DEFAULT TRUE,
  notify_marketing BOOLEAN DEFAULT TRUE,
  notify_review_invites BOOLEAN DEFAULT TRUE,
  profile_hidden BOOLEAN DEFAULT FALSE,
  language TEXT DEFAULT 'ru',
  name TEXT DEFAULT NULL,
  phone TEXT DEFAULT NULL,
  email TEXT DEFAULT NULL,
  avatar_url TEXT DEFAULT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── audit_log ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  admin_chat_id BIGINT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id BIGINT,
  details TEXT,
  admin_username TEXT,
  entity TEXT,
  ip TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── client_otp ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS client_otp (
  id BIGSERIAL PRIMARY KEY,
  phone TEXT NOT NULL,
  code TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  used BOOLEAN DEFAULT FALSE,
  attempts INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── refresh_tokens ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS refresh_tokens (
  id BIGSERIAL PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  admin_id BIGINT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── favorites ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS favorites (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT NOT NULL,
  model_id BIGINT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (chat_id, model_id)
);

-- ── quick_bookings ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS quick_bookings (
  id BIGSERIAL PRIMARY KEY,
  client_name TEXT NOT NULL,
  client_phone TEXT NOT NULL,
  chat_id TEXT,
  status TEXT DEFAULT 'new',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── ab_experiments ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ab_experiments (
  id TEXT PRIMARY KEY,
  hypothesis TEXT NOT NULL,
  type TEXT DEFAULT 'both',
  metric TEXT,
  variant_a TEXT,
  variant_b TEXT,
  effort TEXT DEFAULT 'medium',
  expected_lift TEXT,
  status TEXT DEFAULT 'proposed',
  recommendation TEXT,
  eval_reason TEXT,
  department TEXT DEFAULT 'experiments',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── notifications ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT,
  type TEXT NOT NULL,
  payload TEXT DEFAULT '{}',
  status TEXT DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  sent_at TIMESTAMPTZ DEFAULT NULL
);

-- ── scheduled_broadcasts ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
  id BIGSERIAL PRIMARY KEY,
  text TEXT NOT NULL,
  scheduled_at TIMESTAMPTZ NOT NULL,
  segment TEXT DEFAULT 'all',
  status TEXT DEFAULT 'pending',
  created_by TEXT,
  sent_count INTEGER DEFAULT 0,
  error_count INTEGER DEFAULT 0,
  sent_at TEXT,
  photo_url TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── bot_broadcasts ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bot_broadcasts (
  id BIGSERIAL PRIMARY KEY,
  message TEXT NOT NULL,
  photo_id TEXT,
  segment TEXT DEFAULT 'all',
  sent_by TEXT,
  total_recipients INTEGER DEFAULT 0,
  delivered INTEGER DEFAULT 0,
  failed INTEGER DEFAULT 0,
  skipped INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',
  started_at TIMESTAMPTZ DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);

-- ── totp_temp_tokens ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS totp_temp_tokens (
  id BIGSERIAL PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  admin_id BIGINT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  attempts INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── wishlists ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wishlists (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT NOT NULL,
  model_id BIGINT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (chat_id, model_id)
);

-- ── faq ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS faq (
  id BIGSERIAL PRIMARY KEY,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0,
  active BOOLEAN DEFAULT TRUE,
  category TEXT DEFAULT 'general',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── model_busy_dates ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_busy_dates (
  id BIGSERIAL PRIMARY KEY,
  model_id BIGINT NOT NULL REFERENCES models(id),
  busy_date TEXT NOT NULL,
  reason TEXT,
  order_id BIGINT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (model_id, busy_date)
);

-- ── social_posts ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS social_posts (
  id BIGSERIAL PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'instagram',
  model_id BIGINT REFERENCES models(id) ON DELETE SET NULL,
  content_type TEXT NOT NULL DEFAULT 'post',
  caption TEXT,
  media_url TEXT,
  hashtags TEXT,
  scheduled_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  platform_post_id TEXT UNIQUE,
  metrics TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'draft',
  factory_cycle_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── price_packages ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_packages (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  price_from INTEGER DEFAULT 0,
  price_to INTEGER,
  duration TEXT,
  category TEXT DEFAULT 'standard',
  sort_order INTEGER DEFAULT 0,
  active BOOLEAN DEFAULT TRUE
);

-- ── model_availability ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_availability (
  id BIGSERIAL PRIMARY KEY,
  model_id BIGINT NOT NULL UNIQUE REFERENCES models(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'free',
  date_from TEXT,
  date_to TEXT,
  reason TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── message_templates ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS message_templates (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  text TEXT NOT NULL,
  category TEXT DEFAULT 'general',
  created_by BIGINT,
  use_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── error_logs ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS error_logs (
  id BIGSERIAL PRIMARY KEY,
  level TEXT DEFAULT 'error',
  context TEXT,
  message TEXT NOT NULL,
  stack TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── model_photos ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_photos (
  id BIGSERIAL PRIMARY KEY,
  model_id BIGINT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  url TEXT NOT NULL,
  is_cover BOOLEAN DEFAULT FALSE,
  sort_order INTEGER DEFAULT 0,
  uploaded_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── promo_codes ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promo_codes (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  discount_type TEXT NOT NULL,
  discount_value NUMERIC NOT NULL,
  max_uses INTEGER DEFAULT NULL,
  used_count INTEGER DEFAULT 0,
  valid_from TEXT,
  valid_until TEXT,
  is_active BOOLEAN DEFAULT TRUE,
  created_by BIGINT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── webhook_logs ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS webhook_logs (
  id BIGSERIAL PRIMARY KEY,
  endpoint TEXT NOT NULL,
  payload TEXT,
  status INTEGER,
  ip TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── model_availability_schedule ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_availability_schedule (
  id BIGSERIAL PRIMARY KEY,
  model_id BIGINT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  is_available BOOLEAN DEFAULT TRUE,
  reason TEXT,
  order_id BIGINT REFERENCES orders(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (model_id, date)
);

-- ── support_messages ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS support_messages (
  id BIGSERIAL PRIMARY KEY,
  from_chat_id BIGINT NOT NULL,
  to_chat_id BIGINT,
  message TEXT NOT NULL,
  direction TEXT DEFAULT 'client_to_admin',
  is_read BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- factory_tasks
CREATE INDEX IF NOT EXISTS idx_factory_tasks_status ON factory_tasks(status);
CREATE INDEX IF NOT EXISTS idx_factory_tasks_priority ON factory_tasks(priority DESC, created_at DESC);

-- loyalty
CREATE INDEX IF NOT EXISTS idx_loyalty_chat ON loyalty_points(chat_id);

-- referrals
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_chat_id);

-- achievements
CREATE INDEX IF NOT EXISTS idx_achievements_chat ON achievements(chat_id);

-- audit_log
CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_chat_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(action);

-- client_otp
CREATE INDEX IF NOT EXISTS idx_client_otp_phone ON client_otp(phone, expires_at);
CREATE INDEX IF NOT EXISTS idx_client_otp_expires ON client_otp(expires_at, used);

-- refresh_tokens
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_hash ON refresh_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_admin ON refresh_tokens(admin_id);

-- favorites
CREATE INDEX IF NOT EXISTS idx_favorites_chat ON favorites(chat_id);

-- orders
CREATE INDEX IF NOT EXISTS idx_orders_manager ON orders(manager_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_model_id ON orders(model_id);
CREATE INDEX IF NOT EXISTS idx_orders_client_chat ON orders(client_chat_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_payment_id ON orders(payment_id);
CREATE INDEX IF NOT EXISTS idx_orders_chat_id ON orders(client_chat_id);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status);
CREATE INDEX IF NOT EXISTS idx_orders_client_phone ON orders(client_phone);
CREATE INDEX IF NOT EXISTS idx_orders_event_date ON orders(event_date);
CREATE INDEX IF NOT EXISTS idx_orders_completed_at ON orders(completed_at);
CREATE INDEX IF NOT EXISTS idx_orders_reminded_at ON orders(reminded_at);
CREATE INDEX IF NOT EXISTS idx_orders_review_invitation_sent_at ON orders(review_invitation_sent_at);
CREATE INDEX IF NOT EXISTS idx_orders_reminder_24h ON orders(reminder_24h_sent);
CREATE INDEX IF NOT EXISTS idx_orders_completed_reminder ON orders(completed_reminder_sent);
CREATE INDEX IF NOT EXISTS idx_orders_stars_charge ON orders(stars_payment_charge_id);
CREATE INDEX IF NOT EXISTS idx_orders_promo_code ON orders(promo_code_id);

-- messages
CREATE INDEX IF NOT EXISTS idx_messages_order ON messages(order_id);

-- models
CREATE INDEX IF NOT EXISTS idx_models_category ON models(category);
CREATE INDEX IF NOT EXISTS idx_models_available ON models(available);
CREATE INDEX IF NOT EXISTS idx_models_featured ON models(featured DESC);
CREATE INDEX IF NOT EXISTS idx_models_featured_active ON models(featured) WHERE featured = TRUE;
CREATE INDEX IF NOT EXISTS idx_models_archived ON models(archived);
CREATE INDEX IF NOT EXISTS idx_models_category_active ON models(category) WHERE archived = FALSE;
CREATE INDEX IF NOT EXISTS idx_models_city_active ON models(city) WHERE archived = FALSE;
CREATE INDEX IF NOT EXISTS idx_models_available_active ON models(available) WHERE archived = FALSE;
CREATE INDEX IF NOT EXISTS idx_models_city ON models(city);
CREATE INDEX IF NOT EXISTS idx_models_status ON models(available);

-- telegram_sessions
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON telegram_sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON telegram_sessions(state);
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON telegram_sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_telegram_users_chat_id ON telegram_sessions(chat_id);

-- agent_findings
CREATE INDEX IF NOT EXISTS idx_agent_findings_status ON agent_findings(status);
CREATE INDEX IF NOT EXISTS idx_agent_findings_created ON agent_findings(created_at DESC);

-- agent_discussions
CREATE INDEX IF NOT EXISTS idx_agent_discussions_created ON agent_discussions(created_at DESC);

-- agent_logs
CREATE INDEX IF NOT EXISTS idx_agent_logs_created ON agent_logs(created_at DESC);

-- order_status_history
CREATE INDEX IF NOT EXISTS idx_order_status_history_order ON order_status_history(order_id);
CREATE INDEX IF NOT EXISTS idx_order_status_history_created ON order_status_history(created_at DESC);

-- reviews
CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);
CREATE INDEX IF NOT EXISTS idx_reviews_approved ON reviews(approved);
CREATE INDEX IF NOT EXISTS idx_reviews_model_id ON reviews(model_id);
CREATE INDEX IF NOT EXISTS idx_reviews_client_chat_id ON reviews(chat_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_unique_chat_order ON reviews(chat_id, order_id) WHERE order_id IS NOT NULL;

-- factory_tasks extended
CREATE INDEX IF NOT EXISTS idx_factory_tasks_status_pri ON factory_tasks(status, priority);

-- notifications
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);

-- scheduled_broadcasts
CREATE INDEX IF NOT EXISTS idx_sched_bcast_status ON scheduled_broadcasts(status, scheduled_at);

-- bot_broadcasts
CREATE INDEX IF NOT EXISTS idx_bot_bcast_started ON bot_broadcasts(started_at DESC);

-- totp_temp_tokens
CREATE INDEX IF NOT EXISTS idx_totp_temp_hash ON totp_temp_tokens(token_hash);

-- wishlists
CREATE INDEX IF NOT EXISTS idx_wishlists_chat_id ON wishlists(chat_id);
CREATE INDEX IF NOT EXISTS idx_wishlists_chat_model ON wishlists(chat_id, model_id);
CREATE INDEX IF NOT EXISTS idx_wishlists_chat ON wishlists(chat_id);

-- model_busy_dates
CREATE INDEX IF NOT EXISTS idx_busy_dates_model ON model_busy_dates(model_id, busy_date);

-- social_posts
CREATE INDEX IF NOT EXISTS idx_social_posts_status ON social_posts(status);
CREATE INDEX IF NOT EXISTS idx_social_posts_platform ON social_posts(platform, created_at DESC);

-- model_availability
CREATE INDEX IF NOT EXISTS idx_model_avail_model ON model_availability(model_id);

-- message_templates
CREATE INDEX IF NOT EXISTS idx_msg_tpl_category ON message_templates(category);
CREATE INDEX IF NOT EXISTS idx_msg_tpl_use_count ON message_templates(use_count DESC);

-- error_logs
CREATE INDEX IF NOT EXISTS idx_error_logs_created ON error_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_error_logs_level ON error_logs(level);
CREATE INDEX IF NOT EXISTS idx_error_logs_context ON error_logs(context);

-- model_photos
CREATE INDEX IF NOT EXISTS idx_model_photos_model_id ON model_photos(model_id);
CREATE INDEX IF NOT EXISTS idx_model_photos_cover ON model_photos(model_id, is_cover);

-- promo_codes
CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_promo_codes_active ON promo_codes(is_active);

-- webhook_logs
CREATE INDEX IF NOT EXISTS idx_webhook_logs_created ON webhook_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_logs_endpoint ON webhook_logs(endpoint);

-- model_availability_schedule
CREATE INDEX IF NOT EXISTS idx_mas_model_date ON model_availability_schedule(model_id, date);
CREATE INDEX IF NOT EXISTS idx_mas_date ON model_availability_schedule(date);

-- support_messages
CREATE INDEX IF NOT EXISTS idx_support_messages_from ON support_messages(from_chat_id);
CREATE INDEX IF NOT EXISTS idx_support_messages_direction ON support_messages(direction, created_at DESC);

-- ── Seed bot_settings defaults ─────────────────────────────────────────────────
INSERT INTO bot_settings (key, value) VALUES
  ('catalog_per_page', '8'),
  ('booking_auto_confirm', '0'),
  ('reviews_auto_approve', '0'),
  ('wishlist_enabled', '1'),
  ('search_enabled', '1'),
  ('loyalty_enabled', '1'),
  ('referral_enabled', '1'),
  ('faq_enabled', '1'),
  ('calc_enabled', '1'),
  ('quick_booking_enabled', '1'),
  ('reviews_enabled', '1'),
  ('bot_language', 'ru')
ON CONFLICT (key) DO NOTHING;
