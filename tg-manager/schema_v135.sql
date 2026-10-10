-- schema_v135: DB performance indexes + adaptive pacing columns

-- ── Adaptive pacing columns on operation_queue ─────────────────────────────
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS adaptive_delay FLOAT;
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS hour_of_day INT;

-- ── Performance indexes for frequent query paths ───────────────────────────

-- operation_queue: fast filter by owner + status + created_at (dashboard, op_worker)
CREATE INDEX IF NOT EXISTS idx_op_queue_owner_status_created
    ON operation_queue(owner_id, status, created_at DESC);

-- operation_audit: reports by (account_id, action, occurred_at) — account-level audit
CREATE INDEX IF NOT EXISTS idx_op_audit_account_action
    ON operation_audit(account_id, action, occurred_at DESC);

-- tg_accounts: account list + selection by (owner_id, is_active, trust_score)
CREATE INDEX IF NOT EXISTS idx_tg_accounts_owner_active_trust
    ON tg_accounts(owner_id, is_active, trust_score DESC NULLS LAST);

-- account_flood_log: monitoring by (account_id, created_at)
CREATE INDEX IF NOT EXISTS idx_flood_log_account_created
    ON account_flood_log(account_id, created_at DESC);

-- platform_users: active users lookup by last_seen
CREATE INDEX IF NOT EXISTS idx_platform_users_last_seen
    ON platform_users(last_seen DESC);
