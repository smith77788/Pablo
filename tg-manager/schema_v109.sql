-- schema_v109.sql — Critical performance indexes
-- Fixes full-table-scan bottleneck: auto_replies, automation_rules, funnels
-- queried by auto_responder every 5s for every managed bot without indexes.

-- auto_replies: polled every 5s per bot in _process_bot
CREATE INDEX IF NOT EXISTS idx_auto_replies_bot_active
    ON auto_replies(bot_id) WHERE is_active = TRUE;

-- automation_rules: polled every 5s per bot in _process_bot
CREATE INDEX IF NOT EXISTS idx_automation_rules_bot_active
    ON automation_rules(bot_id) WHERE is_active = TRUE;

-- funnels: polled every 5s per bot in _process_bot
CREATE INDEX IF NOT EXISTS idx_funnels_bot_active
    ON funnels(bot_id) WHERE is_active = TRUE;

-- funnel_steps: joined with funnels, no index on funnel_id
CREATE INDEX IF NOT EXISTS idx_funnel_steps_funnel
    ON funnel_steps(funnel_id);

-- inactivity_alerts_sent: queried in hourly inactivity sweep
CREATE INDEX IF NOT EXISTS idx_inactivity_alerts_bot_rule
    ON inactivity_alerts_sent(bot_id, rule_id, sent_at DESC);

-- ghost_action_log: queried per profile on every ghost engine cycle
CREATE INDEX IF NOT EXISTS idx_ghost_action_log_profile_time
    ON ghost_action_log(ghost_profile_id, executed_at DESC);

-- operation_queue: status filter used heavily by op_worker and handlers
CREATE INDEX IF NOT EXISTS idx_op_queue_status_created
    ON operation_queue(status, created_at) WHERE status IN ('pending', 'running');

-- mesh_queue: already has pending_idx but add mesh+target for delivery lookup
CREATE INDEX IF NOT EXISTS idx_mesh_queue_pending_mesh
    ON mesh_queue(mesh_id, target_id, source_msg_id) WHERE status = 'pending';

-- bot_users: active users lookup in broadcasts and funnel subscriptions
CREATE INDEX IF NOT EXISTS idx_bot_users_active_bot
    ON bot_users(bot_id, is_active) WHERE is_active = TRUE;

-- subscriptions: plan lookup used on every menu render (with 60s cache but cold misses)
CREATE INDEX IF NOT EXISTS idx_subscriptions_active_user
    ON subscriptions(user_id, is_active, expires_at) WHERE is_active = TRUE;
