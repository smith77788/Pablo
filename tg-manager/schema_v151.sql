-- Schema v151: Add missing indexes for frequently queried tables
-- These indexes cover the most common query patterns found in db.py

-- relay_messages: queried by session_id in get_relay_session_messages
CREATE INDEX IF NOT EXISTS idx_relay_messages_session ON relay_messages(session_id, created_at DESC);

-- funnel_subscriptions: queried by funnel_id+completed, funnel_id+user_id, next_send_at
CREATE INDEX IF NOT EXISTS idx_funnel_subs_funnel_completed ON funnel_subscriptions(funnel_id, completed) WHERE completed = FALSE;
CREATE INDEX IF NOT EXISTS idx_funnel_subs_funnel_user ON funnel_subscriptions(funnel_id, user_id);
CREATE INDEX IF NOT EXISTS idx_funnel_subs_next_send ON funnel_subscriptions(next_send_at) WHERE completed = FALSE AND (dropped IS NULL OR dropped = FALSE);

-- experiment_assignments: queried by bot_id+user_id+experiment_id
CREATE INDEX IF NOT EXISTS idx_exp_assignments_bot_user_exp ON experiment_assignments(bot_id, user_id, experiment_id);

-- deep_link_visits: queried by link_id+user_id
CREATE INDEX IF NOT EXISTS idx_deep_link_visits_link_user ON deep_link_visits(link_id, user_id);

-- operation_log: queried by op_id
CREATE INDEX IF NOT EXISTS idx_operation_log_op ON operation_log(op_id);

-- operation_audit: queried by owner_id+occurred_at, account_id+action
CREATE INDEX IF NOT EXISTS idx_operation_audit_owner ON operation_audit(owner_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_audit_account ON operation_audit(account_id, action);

-- platform_referrals: queried by referrer_id+created_at, referred_id
CREATE INDEX IF NOT EXISTS idx_platform_referrals_referrer ON platform_referrals(referrer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_referrals_referred ON platform_referrals(referred_id);

-- bot_deep_links: queried by bot_id+start_param
CREATE INDEX IF NOT EXISTS idx_bot_deep_links_bot_param ON bot_deep_links(bot_id, start_param);

-- broadcasts: add composite index for common query pattern (bot_id + created_at DESC)
CREATE INDEX IF NOT EXISTS idx_broadcasts_bot_created ON broadcasts(bot_id, created_at DESC);

-- funnel_steps: add composite index for ordered retrieval
CREATE INDEX IF NOT EXISTS idx_funnel_steps_funnel_order ON funnel_steps(funnel_id, step_order);
