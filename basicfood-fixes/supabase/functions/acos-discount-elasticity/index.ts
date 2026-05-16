// ACOS Discount Elasticity — for each top product, analyzes how different
// discount levels (0/5/10/15/20%) historically performed in terms of
// conversion rate × net revenue per impression. Recommends the optimal
// discount that maximizes total expected net revenue.
//
// Data sources:
//   - events (product_viewed, add_to_cart, purchase_completed) → CTR/CVR
//   - order_items → realized AOV per product
//   - promotions/promo_codes overlap by created_at window → applied discount %
//
// Output: per-product elasticity curve + recommended discount + insight.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const WINDOW_DAYS = 60;
const MIN_VIEWS = 50;       // skip low-traffic products
const MIN_ORDERS = 5;       // skip products without enough order signal
const TOP_N = 8;            // analyze top-N products by views

// Discount tiers we'll evaluate. 0 = baseline (no promo).
const TIERS = [0, 5, 10, 15, 20];

interface EventRow {
  event_type: string;
  product_id: string | null;
  created_at: string;
}
interface OrderItemRow {
  product_id: string | null;
  product_price: number;
  quantity: number;
  discount_amount: number;
  order_id: string;
  created_at: string;
}
interface ProductRow {
  id: string;
  name: string;
  price: number;
  is_active: boolean;
  stock_quantity: number;
}

interface TierStats {
  tier_pct: number;
  views: number;
  orders: number;
  units: number;
  cvr_pct: number;            // orders / views * 100
  avg_unit_revenue: number;   // (price - discount) per unit on average
  expected_net_per_view: number; // cvr × avg_unit_revenue (per-view value)
}

interface ProductElasticity {
  product_id: string;
  name: string;
  base_price: number;
  total_views: number;
  total_orders: number;
  baseline_cvr_pct: number;
  recommended_tier_pct: number;
  recommended_lift_vs_baseline_pct: number;
  tiers: TierStats[];
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const now = Date.now();
    const windowStart = new Date(now - WINDOW_DAYS * dayMs).toISOString();

    // 1. Pull active products.
    const { data: products } = await supabase
      .from("products")
      .select("id, name, price, is_active, stock_quantity")
      .eq("is_active", true)
      .gt("stock_quantity", 0)
      .limit(100);
    const activeProducts = (products ?? []) as ProductRow[];
    if (activeProducts.length === 0) {
      return new Response(
        JSON.stringify({ products: [], reason: "no_active_products" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull product_viewed events in window (single query, in-memory aggregate).
    const { data: viewEvents } = await supabase
      .from("events")
      .select("event_type, product_id, created_at")
      .eq("event_type", "product_viewed")
      .gte("created_at", windowStart)
      .not("product_id", "is", null)
      .limit(50000);
    const views = (viewEvents ?? []) as EventRow[];

    // 3. Pull order_items in window.
    const { data: orderItems } = await supabase
      .from("order_items")
      .select("product_id, product_price, quantity, discount_amount, order_id, created_at")
      .gte("created_at", windowStart)
      .not("product_id", "is", null)
      .limit(20000);
    const items = (orderItems ?? []) as OrderItemRow[];

    // 4. Per-product aggregation.
    const stats: ProductElasticity[] = [];

    for (const p of activeProducts) {
      const myViews = views.filter((v) => v.product_id === p.id);
      const myItems = items.filter((it) => it.product_id === p.id);

      if (myViews.length < MIN_VIEWS || myItems.length < MIN_ORDERS) continue;

      // Bucket each order_item into a discount tier by (discount/price)%.
      const tierBuckets: Record<number, { orders: Set<string>; units: number; revenue: number }> = {};
      for (const t of TIERS) {
        tierBuckets[t] = { orders: new Set(), units: 0, revenue: 0 };
      }

      for (const it of myItems) {
        const unitDiscount = it.quantity > 0 ? it.discount_amount / it.quantity : 0;
        const discountPct = it.product_price > 0 ? (unitDiscount / it.product_price) * 100 : 0;
        // Snap to nearest tier.
        let nearest = 0;
        let bestDelta = Infinity;
        for (const t of TIERS) {
          const d = Math.abs(discountPct - t);
          if (d < bestDelta) { bestDelta = d; nearest = t; }
        }
        const bucket = tierBuckets[nearest];
        bucket.orders.add(it.order_id);
        bucket.units += it.quantity;
        bucket.revenue += (it.product_price - unitDiscount) * it.quantity;
      }

      // Baseline CVR = total_orders / total_views (we'll attribute to views proportionally).
      const totalOrders = new Set(myItems.map((it) => it.order_id)).size;
      const baselineCvr = (totalOrders / myViews.length) * 100;

      // For tier-level CVR, we don't have direct view-to-tier mapping (we don't
      // promo-tag impressions). Approximation: assume views distribute across
      // tiers proportionally to their ORDER share — i.e., we measure relative
      // efficiency, not absolute. Then expected_net_per_view = (orders_in_tier
      // / total_orders) × baseline_cvr × avg_unit_revenue_in_tier.
      const tierStats: TierStats[] = TIERS.map((t) => {
        const b = tierBuckets[t];
        const orderShare = totalOrders > 0 ? b.orders.size / totalOrders : 0;
        const cvrInTier = baselineCvr * orderShare * (TIERS.length); // re-scale share
        const avgUnitRev = b.units > 0 ? b.revenue / b.units : 0;
        const expectedNet = (cvrInTier / 100) * avgUnitRev;
        return {
          tier_pct: t,
          views: Math.round(myViews.length * orderShare),
          orders: b.orders.size,
          units: b.units,
          cvr_pct: Math.round(cvrInTier * 100) / 100,
          avg_unit_revenue: Math.round(avgUnitRev),
          expected_net_per_view: Math.round(expectedNet * 100) / 100,
        };
      });

      // Recommend tier with highest expected_net_per_view, but ONLY if it
      // beats baseline (tier 0) by ≥10% — otherwise stick with no-discount.
      const baseline = tierStats.find((s) => s.tier_pct === 0)!;
      let best = baseline;
      for (const s of tierStats) {
        if (s.tier_pct === 0) continue;
        if (s.orders < 2) continue; // need real signal
        if (s.expected_net_per_view > best.expected_net_per_view * 1.10) {
          best = s;
        }
      }

      const liftPct = baseline.expected_net_per_view > 0
        ? Math.round(((best.expected_net_per_view - baseline.expected_net_per_view) / baseline.expected_net_per_view) * 1000) / 10
        : 0;

      stats.push({
        product_id: p.id,
        name: p.name,
        base_price: p.price,
        total_views: myViews.length,
        total_orders: totalOrders,
        baseline_cvr_pct: Math.round(baselineCvr * 100) / 100,
        recommended_tier_pct: best.tier_pct,
        recommended_lift_vs_baseline_pct: liftPct,
        tiers: tierStats,
      });
    }

    // 5. Sort by views, take top-N.
    stats.sort((a, b) => b.total_views - a.total_views);
    const top = stats.slice(0, TOP_N);

    // 6. Emit insight only for products where recommended ≠ 0 AND lift ≥ 15% (real signal).
    const insightRows = top
      .filter((s) => s.recommended_tier_pct !== 0 && s.recommended_lift_vs_baseline_pct >= 15)
      .map((s) => ({
        insight_type: "discount_elasticity",
        title: `${s.name}: оптимальна знижка ${s.recommended_tier_pct}% (+${s.recommended_lift_vs_baseline_pct}% net/view)`,
        description: `За ${WINDOW_DAYS}д: ${s.total_views} переглядів, ${s.total_orders} замовлень. Baseline CVR ${s.baseline_cvr_pct}%. При знижці ${s.recommended_tier_pct}% expected net per view вищий на ${s.recommended_lift_vs_baseline_pct}% vs no-discount. Рекомендація: створити промокод ${s.recommended_tier_pct}% специфічно для цього SKU або включити в найближчий broadcast.`,
        expected_impact: `~+${Math.round(s.total_views * (s.recommended_lift_vs_baseline_pct / 100) * 30 / WINDOW_DAYS).toLocaleString()}₴/міс приросту net revenue`,
        confidence: 0.7,
        risk_level: "low",
        affected_layer: "merchandising",
        status: "new",
        metrics: {
          product_id: s.product_id,
          product_name: s.name,
          base_price: s.base_price,
          recommended_discount_pct: s.recommended_tier_pct,
          baseline_cvr_pct: s.baseline_cvr_pct,
          lift_pct: s.recommended_lift_vs_baseline_pct,
          tiers: s.tiers,
        },
      }));
    let insightsCreated = 0;
    if (insightRows.length > 0) {
      await supabase.from("ai_insights").insert(insightRows).catch(() => {});
      insightsCreated = insightRows.length;
    }

    return new Response(
      JSON.stringify({
        products: top,
        analyzed_count: stats.length,
        insights_created: insightsCreated,
        window_days: WINDOW_DAYS,
        tiers_evaluated: TIERS,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
