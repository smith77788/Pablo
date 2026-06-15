-- schema_v100: performance indexes for high-traffic menu queries

-- operation_audit: cb_main stats (occurred_at filter per owner)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_op_audit_owner_date
    ON operation_audit (owner_id, occurred_at DESC);

-- operation_queue: cb_main + queue dashboard (owner + status + created_at)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_op_queue_owner_status
    ON operation_queue (owner_id, status, created_at DESC);

-- tg_accounts: account list + cooldown check
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tg_acc_owner_active
    ON tg_accounts (owner_id, is_active, trust_score DESC NULLS LAST);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tg_acc_cooldown
    ON tg_accounts (owner_id, cooldown_until)
    WHERE cooldown_until IS NOT NULL;

-- restriction_events: cb_main new_alerts (24h window)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_restr_events_owner_time
    ON restriction_events (owner_id, created_at DESC);

-- managed_bots: bot list (most common query)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_managed_bots_owner
    ON managed_bots (owner_id, created_at DESC);

-- bot_warehouse: promo platform warehouse list
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_bot_warehouse_owner_status
    ON bot_warehouse (owner_id, status);

-- promo_orders: order list by owner+status
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_promo_orders_owner_status
    ON promo_orders (owner_id, status, created_at DESC);

-- promo_logs: log view by owner (most recent first)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_promo_logs_owner_time
    ON promo_logs (owner_id, created_at DESC);

-- platform_users: subscription checks (very frequent)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_platform_users_id
    ON platform_users (user_id);
