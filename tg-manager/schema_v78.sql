-- schema_v78: performance indexes for hot query paths

-- operation_queue: cb_main reads today's ops (created_at >= CURRENT_DATE)
CREATE INDEX IF NOT EXISTS idx_op_queue_owner_created
    ON operation_queue(owner_id, created_at DESC);

-- tg_accounts: cooldown filter used in cb_main and resource_selector
CREATE INDEX IF NOT EXISTS idx_tg_accounts_cooldown
    ON tg_accounts(owner_id, cooldown_until)
    WHERE cooldown_until IS NOT NULL;

-- subscriptions: plan lookup includes is_active + expires_at conditions
CREATE INDEX IF NOT EXISTS idx_subscriptions_plan_lookup
    ON subscriptions(user_id, is_active, expires_at)
    WHERE is_active = true;

-- operation_queue: partial index for active-ops polling (pending/running)
CREATE INDEX IF NOT EXISTS idx_op_queue_active
    ON operation_queue(owner_id, created_at DESC)
    WHERE status IN ('pending', 'running');

-- global_presence_targets: pending targets fetched per plan in op_worker
CREATE INDEX IF NOT EXISTS idx_gpt_plan_pending
    ON global_presence_targets(plan_id, id)
    WHERE status = 'pending';
