CREATE INDEX IF NOT EXISTS idx_operation_queue_owner_status ON operation_queue(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_operation_queue_scheduled ON operation_queue(scheduled_for) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tg_accounts_owner_active ON tg_accounts(owner_id, is_active);
CREATE INDEX IF NOT EXISTS idx_tg_accounts_cooldown_active ON tg_accounts(cooldown_until) WHERE cooldown_until > NOW();
CREATE INDEX IF NOT EXISTS idx_channel_members_user ON channel_members(user_id);
CREATE INDEX IF NOT EXISTS idx_channel_members_channel ON channel_members(channel_id);
CREATE INDEX IF NOT EXISTS idx_warmup_sessions_status ON warmup_sessions(status) WHERE status = 'active';
