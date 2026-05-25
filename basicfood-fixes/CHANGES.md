# BASIC.FOOD Bug Fixes & Security Hardening

Apply these files to the Lovable project by replacing the corresponding files.

## CRITICAL — Run These Migrations First

### `supabase/migrations/20260512000001_fix_orders_missing_columns.sql`
**Fixes checkout failure "Тимчасова технічна помилка на сервері".**
The `create_order_with_items` RPC references 5 columns that don't exist in the `orders` table. This migration adds them:
- `delivery_method`, `notes`, `total_amount`, `reorder_plan_id`, `subscription_discount`

Run in Supabase SQL Editor **immediately** — customers cannot place orders without this.

### `supabase/migrations/20260512000002_performance_indexes.sql`
Adds 8 missing database indexes that prevent N+1 query slowdowns:
- Telegram cart lookups, active product sorting, event queries, order status queries, etc.

Run during low traffic (indexes use `IF NOT EXISTS` so safe to re-run).

---

## Security Fixes (Edge Functions)

### Auth gate added (`requireInternalCaller`) — prevents public internet abuse:
| File | Risk without fix |
|------|-----------------|
| `supabase/functions/ai-daily-brief/index.ts` | Anyone could trigger AI requests + spam admin Telegram |
| `supabase/functions/cancel-stale-orders/index.ts` | Anyone could cancel real customer orders |
| `supabase/functions/ai-weekly-digest/index.ts` | Anyone could trigger AI requests + spam Telegram |
| `supabase/functions/checkout-failure-watcher/index.ts` | Anyone could trigger internal monitoring |
| `supabase/functions/toxic-pattern-alerter/index.ts` | Anyone could trigger Telegram spam to admins |
| `supabase/functions/instagram-sync/index.ts` | Anyone could trigger external RSS fetch + DB writes |
| `supabase/functions/outreach-roi-collector/index.ts` | Anyone could trigger cron analytics |
| `supabase/functions/generate-content/index.ts` | Anyone could burn Gemini API quota |
| `supabase/functions/respeecher-tts/index.ts` | Anyone could burn Respeecher TTS quota |

### Rate limiting added (DoS protection):
| File | Limit |
|------|-------|
| `supabase/functions/acos-social-proof/index.ts` | 60 req/min per IP |
| `supabase/functions/debug-report/index.ts` | 10 req/min per IP |
| `supabase/functions/translate-content/index.ts` | 20 req/min per IP |

---

## Performance Fixes (Edge Functions)

### `supabase/functions/admin-telegram/index.ts`
**Broadcast was sequential** — 500 recipients × 200ms = 100 second timeout. Now sends in **batches of 25 in parallel** with 1.1s between batches (respects Telegram's 30 msg/s global limit).

### `supabase/functions/acos-cart-recovery/index.ts`
**N+1 product name query** inside loop → single batch fetch before loop.

### `supabase/functions/notify-telegram/index.ts`
Sequential admin notification loop → `Promise.all` parallel sends.

---

## Bug Fixes (Edge Functions)

### `supabase/functions/_shared/ai-router.ts`
`import { sanitizePlaceholders }` was placed after the `routeAI` export — invalid ES module syntax causing Deno runtime error. Moved to top of file.

### `supabase/functions/ai-chat/index.ts`
Invalid Gemini model names (`gemini-flash-latest`, `gemini-2.5-flash-lite` don't exist). Fixed to valid model IDs.

### `supabase/functions/generate-content/index.ts`
Same invalid Gemini model names fix.

### `supabase/functions/acos-winback/index.ts`
When Telegram send fails, promo code was left active but undelivered (orphaned). Now deactivates the code immediately on send failure.

---

## Frontend Fixes

### `src/pages/admin/AdminOrders.tsx`
Tracking number input was missing `disabled={updateOrder.isPending}` — admin could double-submit while save was in progress.

### `src/pages/DogAdvisorQuizPage.tsx` & `src/pages/BuildYourBoxPage.tsx`
Missing `.catch()` on product load — if network failed, `setLoading(false)` was never called, leaving users stuck on a spinner. Fixed with `.catch().finally()`.

### `src/components/NovaPoshtaPicker.tsx`
Warehouse/city dropdown had `z-index: 30` but was hidden under the checkout sticky footer at `z-index: 40`. Changed to `z-index: 50`.
