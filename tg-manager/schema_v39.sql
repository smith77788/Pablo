-- schema_v39.sql: User Management & Admin Audit

-- ── Platform Users (учёт всех пользователей)
CREATE TABLE IF NOT EXISTS platform_users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    registered_at TIMESTAMPTZ DEFAULT now(),
    last_seen TIMESTAMPTZ DEFAULT now(),
    current_plan TEXT DEFAULT 'free',
    plan_expires_at TIMESTAMPTZ,
    is_banned BOOLEAN DEFAULT false,
    ban_reason TEXT,
    banned_at TIMESTAMPTZ,
    referrer_id BIGINT REFERENCES platform_users(user_id) ON DELETE SET NULL,
    notes TEXT
);

-- Add missing columns if table already exists (schema_v14 had last_active/first_seen/is_blocked)
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ DEFAULT now();
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS current_plan TEXT DEFAULT 'free';
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMPTZ;
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT false;
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS ban_reason TEXT;
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ;
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS referrer_id BIGINT REFERENCES platform_users(user_id) ON DELETE SET NULL;
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS notes TEXT;

-- Backfill from old column names if they exist
UPDATE platform_users SET last_seen = last_active WHERE last_seen IS NULL AND last_active IS NOT NULL;
UPDATE platform_users SET registered_at = first_seen WHERE registered_at IS NULL AND first_seen IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_platform_users_plan ON platform_users(current_plan);
CREATE INDEX IF NOT EXISTS idx_platform_users_registered ON platform_users(registered_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_users_is_banned ON platform_users(is_banned);
CREATE INDEX IF NOT EXISTS idx_platform_users_plan_expires ON platform_users(plan_expires_at);

-- ── Admin Audit Log (логирование всех админ-действий)
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id BIGSERIAL PRIMARY KEY,
    admin_id BIGINT NOT NULL REFERENCES platform_users(user_id),
    action TEXT NOT NULL,  -- grant_plan, revoke_plan, ban_user, edit_price, edit_payment_method
    target_user_id BIGINT REFERENCES platform_users(user_id),
    details JSONB DEFAULT '{}',  -- {plan: "pro", months: 3, expires_at: "...", old_value: "...", new_value: "..."}
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_admin_id ON admin_audit_log(admin_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit_log(target_user_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at DESC);

-- ── Unauthorized Access Attempts (попытки несанкционированного доступа)
CREATE TABLE IF NOT EXISTS security_violations (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    attempt_type TEXT NOT NULL,  -- unauthorized_admin_access, invalid_token, suspicious_activity
    details JSONB,
    ip_address TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_security_violations_user ON security_violations(user_id);
CREATE INDEX IF NOT EXISTS idx_security_violations_type ON security_violations(attempt_type);
CREATE INDEX IF NOT EXISTS idx_security_violations_created ON security_violations(created_at DESC);

-- ── Extend managed_bots для связи с user (если её нет)
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS added_by BIGINT REFERENCES platform_users(user_id) ON DELETE SET NULL;

-- ── Extend tg_accounts для связи с user (если её нет)
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS added_by BIGINT REFERENCES platform_users(user_id) ON DELETE SET NULL;
