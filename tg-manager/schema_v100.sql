-- schema_v100: performance indexes for high-traffic menu queries
-- Wrapped in DO block so individual failures don't abort the whole migration

DO $$
BEGIN

  -- operation_audit: cb_main daily stats per owner
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='operation_audit') THEN
    CREATE INDEX IF NOT EXISTS idx_op_audit_owner_date
        ON operation_audit (owner_id, occurred_at DESC);
  END IF;

  -- operation_queue: queue dashboard per owner+status
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='operation_queue') THEN
    CREATE INDEX IF NOT EXISTS idx_op_queue_owner_status
        ON operation_queue (owner_id, status, created_at DESC);
  END IF;

  -- tg_accounts: account list + cooldown checks
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='tg_accounts') THEN
    CREATE INDEX IF NOT EXISTS idx_tg_acc_owner_active
        ON tg_accounts (owner_id, is_active, trust_score DESC NULLS LAST);
    CREATE INDEX IF NOT EXISTS idx_tg_acc_cooldown
        ON tg_accounts (owner_id, cooldown_until)
        WHERE cooldown_until IS NOT NULL;
  END IF;

  -- restriction_events: cb_main new-alerts (24h window)
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='restriction_events') THEN
    CREATE INDEX IF NOT EXISTS idx_restr_events_owner_time
        ON restriction_events (owner_id, created_at DESC);
  END IF;

  -- managed_bots: bot list per owner (column is added_by, not owner_id)
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='managed_bots') THEN
    CREATE INDEX IF NOT EXISTS idx_managed_bots_owner
        ON managed_bots (added_by, added_at DESC);
  END IF;

  -- bot_warehouse: promo warehouse list per owner+status
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='bot_warehouse') THEN
    CREATE INDEX IF NOT EXISTS idx_bot_warehouse_owner_status
        ON bot_warehouse (owner_id, status);
  END IF;

  -- promo_orders: order list per owner+status
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='promo_orders') THEN
    CREATE INDEX IF NOT EXISTS idx_promo_orders_owner_status
        ON promo_orders (owner_id, status, created_at DESC);
  END IF;

  -- promo_logs: log view per owner (newest first)
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='promo_logs') THEN
    CREATE INDEX IF NOT EXISTS idx_promo_logs_owner_time
        ON promo_logs (owner_id, created_at DESC);
  END IF;

  -- platform_users: subscription checks (extremely frequent)
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='platform_users') THEN
    CREATE INDEX IF NOT EXISTS idx_platform_users_uid
        ON platform_users (user_id);
  END IF;

END $$;
