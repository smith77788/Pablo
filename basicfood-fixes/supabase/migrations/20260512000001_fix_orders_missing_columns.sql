-- Fix: checkout "Тимчасова технічна помилка" — add columns required by
-- create_order_with_items RPC (migration 20260510151347) that were never
-- added to the orders table.
--
-- Root cause: the RPC inserts into delivery_method / notes / total_amount /
-- reorder_plan_id / subscription_discount, but those columns didn't exist,
-- causing PostgreSQL error 42703 (undefined_column) on every order attempt.

ALTER TABLE public.orders
  ADD COLUMN IF NOT EXISTS delivery_method       text    NOT NULL DEFAULT 'nova_poshta',
  ADD COLUMN IF NOT EXISTS notes                 text,
  ADD COLUMN IF NOT EXISTS total_amount          integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS reorder_plan_id       uuid    REFERENCES public.reorder_plans(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS subscription_discount integer NOT NULL DEFAULT 0;

-- Back-fill total_amount from existing total column so reports stay consistent
UPDATE public.orders SET total_amount = total WHERE total_amount = 0 AND total > 0;

-- Back-fill notes from existing message column
UPDATE public.orders SET notes = message WHERE notes IS NULL AND message IS NOT NULL AND message <> '';

-- Index for reorder plan lookups
CREATE INDEX IF NOT EXISTS idx_orders_reorder_plan ON public.orders(reorder_plan_id)
  WHERE reorder_plan_id IS NOT NULL;
