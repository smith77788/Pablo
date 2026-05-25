// ACOS Product Bundle Recommender — analyzes order_items co-occurrence
// over the last 90 days and suggests the top-5 product pairs that are
// bought together significantly more often than chance (lift > 1.5).
// Emits a single roll-up insight with the proposed bundle list.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const WINDOW_DAYS = 90;
const MIN_PAIR_ORDERS = 5;     // pair must appear in >=5 orders
const MIN_LIFT = 1.5;          // lift threshold for "real" affinity
const TOP_N = 5;

interface ItemRow {
  order_id: string;
  product_id: string | null;
  product_name: string;
  product_price: number;
  quantity: number;
}

interface PairStat {
  a_id: string;
  b_id: string;
  a_name: string;
  b_name: string;
  pair_orders: number;
  a_orders: number;
  b_orders: number;
  support: number;
  lift: number;
  combined_price: number;
  suggested_bundle_price: number;
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
    const since = new Date(Date.now() - WINDOW_DAYS * 24 * 60 * 60 * 1000).toISOString();

    // 1. Pull paid orders in window (exclude spin_game prizes).
    const { data: orders } = await supabase
      .from("orders")
      .select("id")
      .gte("created_at", since)
      .neq("source", "spin_game")
      .in("status", ["new", "confirmed", "shipped", "delivered", "paid"])
      .limit(5000);

    const orderIds = (orders ?? []).map((o) => o.id);
    if (orderIds.length < 20) {
      return new Response(
        JSON.stringify({ orders: orderIds.length, pairs: 0, reason: "not_enough_orders" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull all order_items for those orders in parallel chunks (avoid IN limit).
    const chunkSize = 500;
    const chunks: string[][] = [];
    for (let i = 0; i < orderIds.length; i += chunkSize) chunks.push(orderIds.slice(i, i + chunkSize));
    const chunkResults = await Promise.all(
      chunks.map((chunk) =>
        supabase.from("order_items")
          .select("order_id, product_id, product_name, product_price, quantity")
          .in("order_id", chunk)
      ),
    );
    const items: ItemRow[] = chunkResults.flatMap((r) => (r.data as ItemRow[]) ?? []);

    if (items.length === 0) {
      return new Response(
        JSON.stringify({ orders: orderIds.length, pairs: 0, reason: "no_items" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 3. Group items by order; collect distinct product set per order.
    const orderProducts = new Map<string, Set<string>>();
    const productNames = new Map<string, string>();
    const productPrices = new Map<string, number>();

    for (const it of items) {
      if (!it.product_id) continue;
      productNames.set(it.product_id, it.product_name);
      productPrices.set(it.product_id, it.product_price);
      const set = orderProducts.get(it.order_id) ?? new Set<string>();
      set.add(it.product_id);
      orderProducts.set(it.order_id, set);
    }

    // 4. Count single-product order frequency + pair co-occurrence.
    const singleCount = new Map<string, number>();
    const pairCount = new Map<string, number>();

    for (const set of orderProducts.values()) {
      const arr = Array.from(set);
      for (const p of arr) {
        singleCount.set(p, (singleCount.get(p) ?? 0) + 1);
      }
      // Generate unordered pairs.
      for (let i = 0; i < arr.length; i++) {
        for (let j = i + 1; j < arr.length; j++) {
          const [a, b] = arr[i] < arr[j] ? [arr[i], arr[j]] : [arr[j], arr[i]];
          const key = `${a}|${b}`;
          pairCount.set(key, (pairCount.get(key) ?? 0) + 1);
        }
      }
    }

    const totalOrders = orderProducts.size;

    // 5. Compute lift for every pair that meets MIN_PAIR_ORDERS.
    const pairs: PairStat[] = [];
    for (const [key, count] of pairCount.entries()) {
      if (count < MIN_PAIR_ORDERS) continue;
      const [a, b] = key.split("|");
      const aCount = singleCount.get(a) ?? 0;
      const bCount = singleCount.get(b) ?? 0;
      if (aCount === 0 || bCount === 0) continue;

      const support = count / totalOrders;
      const expected = (aCount / totalOrders) * (bCount / totalOrders);
      const lift = expected > 0 ? support / expected : 0;

      if (lift < MIN_LIFT) continue;

      const aPrice = productPrices.get(a) ?? 0;
      const bPrice = productPrices.get(b) ?? 0;
      const combined = aPrice + bPrice;
      // Suggest 10% bundle discount.
      const bundlePrice = Math.round(combined * 0.9);

      pairs.push({
        a_id: a,
        b_id: b,
        a_name: productNames.get(a) ?? "?",
        b_name: productNames.get(b) ?? "?",
        pair_orders: count,
        a_orders: aCount,
        b_orders: bCount,
        support: Math.round(support * 1000) / 1000,
        lift: Math.round(lift * 100) / 100,
        combined_price: combined,
        suggested_bundle_price: bundlePrice,
      });
    }

    // 6. Sort by lift × pair_orders (strong + frequent) and take top N.
    pairs.sort((x, y) => y.lift * y.pair_orders - x.lift * x.pair_orders);
    const top = pairs.slice(0, TOP_N);

    // 7. Emit insight if we have at least one strong pair.
    if (top.length > 0) {
      const summary = top
        .map(
          (p, i) =>
            `${i + 1}. ${p.a_name} + ${p.b_name} — ${p.pair_orders} разом (lift ${p.lift}×), bundle ${p.suggested_bundle_price}₴ замість ${p.combined_price}₴`,
        )
        .join("\n");

      await supabase.from("ai_insights").insert({
        insight_type: "product_bundle_suggestions",
        title: `${top.length} bundle-комбінацій з високим lift (топ ${top[0].lift}×)`,
        description: `Аналіз ${totalOrders} замовлень за ${WINDOW_DAYS} днів виявив пари товарів, які купують разом значно частіше за випадковість (lift ≥ ${MIN_LIFT}). Топ кандидати на bundle промоції:\n\n${summary}`,
        expected_impact: `+8-15% AOV на bundle-замовленнях, очікуваний lift виручки ~${Math.round(top.reduce((s, p) => s + p.suggested_bundle_price * p.pair_orders * 0.1, 0)).toLocaleString()}₴/міс`,
        confidence: 0.75,
        risk_level: "low",
        affected_layer: "merchandising",
        status: "new",
        metrics: {
          window_days: WINDOW_DAYS,
          total_orders: totalOrders,
          pairs_analyzed: pairCount.size,
          pairs_meeting_threshold: pairs.length,
          top_pairs: top,
        },
      });
    }

    return new Response(
      JSON.stringify({
        orders: totalOrders,
        pairs_analyzed: pairCount.size,
        pairs_meeting_threshold: pairs.length,
        top: top,
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
