-- Performance indexes — fixes N+1 query slowdowns identified in audit
-- Safe to run on live DB: CREATE INDEX CONCURRENTLY doesn't lock table.
-- (Supabase SQL Editor doesn't support CONCURRENTLY — run during low traffic.)

-- Composite index for telegram bot cart lookups (chat_id + product_id)
CREATE INDEX IF NOT EXISTS idx_telegram_carts_chat_product
  ON public.telegram_carts(chat_id, product_id);

-- Partial index for active products sorted by popularity (ai-chat catalog query)
CREATE INDEX IF NOT EXISTS idx_products_active_sold
  ON public.products(sold_count DESC)
  WHERE is_active = true AND stock_quantity > 0;

-- Index for events cart recovery query (event_type + user_id + created_at)
CREATE INDEX IF NOT EXISTS idx_events_type_user_time
  ON public.events(event_type, user_id, created_at DESC)
  WHERE user_id IS NOT NULL;

-- Index for orders reorder_plan_id (new column added in 20260512000001)
CREATE INDEX IF NOT EXISTS idx_orders_reorder_plan
  ON public.orders(reorder_plan_id)
  WHERE reorder_plan_id IS NOT NULL;

-- Index for orders total_amount for revenue analytics queries
CREATE INDEX IF NOT EXISTS idx_orders_status_created
  ON public.orders(status, created_at DESC);

-- Index for review_requests by product + status
CREATE INDEX IF NOT EXISTS idx_review_requests_product_status
  ON public.review_requests(product_id, status);

-- Index for promo_code_usages by promo_code_id
CREATE INDEX IF NOT EXISTS idx_promo_code_usages_code
  ON public.promo_code_usages(promo_code_id);

-- Index for personal_concierge_log (user + recency) for throttle check
CREATE INDEX IF NOT EXISTS idx_pcl_user_created
  ON public.personal_concierge_log(user_id, created_at DESC);

-- Partial index for open debug reports (checkout-failure-watcher uses this)
CREATE INDEX IF NOT EXISTS idx_debug_reports_open
  ON public.debug_reports(created_at DESC)
  WHERE resolved_at IS NULL;
