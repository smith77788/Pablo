// ACOS Predictive Pricing Engine
// Analyzes historical promotion performance to estimate price elasticity per product
// and recommends an optimal price that maximizes revenue.
//
// Method (heuristic, no ML deps):
//   1. For each active product, compute baseline daily velocity (units/day) over last 60 days
//      from order_items joined to orders (excluding cancelled).
//   2. Find past promotion windows that touched this product (via product_ids OR category_filter)
//      and compute promo-period velocity vs. baseline → implied elasticity.
//   3. Recommend price using midpoint of safe elasticity band:
//        - High elasticity (>1.5): suggest -10% (volume play)
//        - Mid (0.5..1.5): hold price
//        - Low (<0.5) + sold_count<5/wk: suggest +5% (margin play)
//   4. Compute expected_revenue_lift = (new_price * predicted_units) - (old_price * baseline_units)
//   5. Persist as ai_insights of type "pricing_recommendation".

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const ORDER_OK = ["new", "processing", "shipped", "delivered", "completed"];

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const now = Date.now();
    const t60 = new Date(now - 60 * 86400_000).toISOString();
    const nowIso = new Date(now).toISOString();

    // Active products
    const { data: products } = await supabase
      .from("products")
      .select("id, name, price, sold_count, stock_quantity, categories")
      .eq("is_active", true);
    if (!products || products.length === 0) {
      return new Response(JSON.stringify({ scanned: 0, reason: "no_products" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Fetch all order_items in last 60 days with parent order status
    const { data: items } = await supabase
      .from("order_items")
      .select("product_id, quantity, product_price, created_at, order_id")
      .gte("created_at", t60);

    const orderIds = [...new Set((items ?? []).map((i) => i.order_id))];
    let validOrderSet = new Set<string>();
    if (orderIds.length > 0) {
      const chunkSize = 200;
      const chunks: string[][] = [];
      for (let i = 0; i < orderIds.length; i += chunkSize) chunks.push(orderIds.slice(i, i + chunkSize));
      const chunkResults = await Promise.all(
        chunks.map((chunk) =>
          supabase.from("orders").select("id, status, created_at").in("id", chunk)
        ),
      );
      for (const { data: ords } of chunkResults) {
        for (const o of ords ?? []) {
          if (ORDER_OK.includes(o.status)) validOrderSet.add(o.id);
        }
      }
    }

    // Past promotions in last 60d
    const { data: promotions } = await supabase
      .from("promotions")
      .select("product_ids, category_filter, starts_at, ends_at, discount_value, discount_type")
      .gte("ends_at", t60)
      .lte("starts_at", nowIso);

    const recommendations: Array<{
      product_id: string;
      product_name: string;
      current_price: number;
      recommended_price: number;
      elasticity: number;
      action: "increase" | "decrease" | "hold";
      baseline_units_per_day: number;
      promo_units_per_day: number;
      expected_revenue_lift_30d: number;
    }> = [];

    for (const p of products) {
      const productItems = (items ?? []).filter(
        (it) => it.product_id === p.id && validOrderSet.has(it.order_id ?? ""),
      );
      const totalUnits = productItems.reduce((s, it) => s + (it.quantity ?? 0), 0);
      const baselineVelocity = totalUnits / 60;

      // Compute promo-period velocity for promotions that touched this product
      let promoUnits = 0;
      let promoDays = 0;
      for (const pr of promotions ?? []) {
        const touches =
          (pr.product_ids ?? []).includes(p.id) ||
          (pr.category_filter ?? []).some((c: string) => (p.categories ?? []).includes(c));
        if (!touches || !pr.starts_at || !pr.ends_at) continue;
        const start = new Date(pr.starts_at).getTime();
        const end = Math.min(new Date(pr.ends_at).getTime(), now);
        if (end <= start) continue;
        const days = (end - start) / 86400_000;
        promoDays += days;
        const inWindow = productItems.filter((it) => {
          const t = new Date(it.created_at).getTime();
          return t >= start && t <= end;
        });
        promoUnits += inWindow.reduce((s, it) => s + (it.quantity ?? 0), 0);
      }
      const promoVelocity = promoDays > 0 ? promoUnits / promoDays : baselineVelocity;

      // Elasticity ≈ (%ΔQ) / (%ΔP). With avg promo discount assumed ~15% if promotions ran.
      const avgDiscount = 0.15;
      const elasticity =
        promoDays > 0 && baselineVelocity > 0
          ? Math.max(0, (promoVelocity / baselineVelocity - 1) / avgDiscount)
          : 0;

      let action: "increase" | "decrease" | "hold" = "hold";
      let newPrice = p.price;
      let predictedVelocity = baselineVelocity;
      if (elasticity > 1.5 && baselineVelocity >= 0.2) {
        action = "decrease";
        newPrice = Math.round(p.price * 0.9);
        predictedVelocity = baselineVelocity * (1 + 0.1 * elasticity);
      } else if (elasticity < 0.5 && baselineVelocity < 0.7 && (p.stock_quantity ?? 0) > 5) {
        action = "increase";
        newPrice = Math.round(p.price * 1.05);
        predictedVelocity = baselineVelocity * (1 - 0.05 * Math.max(0.2, elasticity));
      }

      const baselineRev30 = baselineVelocity * 30 * p.price;
      const newRev30 = predictedVelocity * 30 * newPrice;
      const lift = Math.round(newRev30 - baselineRev30);

      // Skip noise: only persist when meaningful change recommended OR notable elasticity
      if (action === "hold" && Math.abs(elasticity) < 0.3) continue;

      recommendations.push({
        product_id: p.id,
        product_name: p.name,
        current_price: p.price,
        recommended_price: newPrice,
        elasticity: Number(elasticity.toFixed(2)),
        action,
        baseline_units_per_day: Number(baselineVelocity.toFixed(2)),
        promo_units_per_day: Number(promoVelocity.toFixed(2)),
        expected_revenue_lift_30d: lift,
      });
    }

    // Sort by absolute revenue lift descending
    recommendations.sort(
      (a, b) => Math.abs(b.expected_revenue_lift_30d) - Math.abs(a.expected_revenue_lift_30d),
    );
    const top = recommendations.slice(0, 10);

    if (top.length > 0) {
      const totalLift = top.reduce((s, r) => s + r.expected_revenue_lift_30d, 0);
      const lines = top
        .slice(0, 5)
        .map(
          (r) =>
            `${r.action === "decrease" ? "📉" : r.action === "increase" ? "📈" : "➡️"} ${r.product_name}: ${r.current_price}→${r.recommended_price}₴ (E=${r.elasticity}, lift ${r.expected_revenue_lift_30d >= 0 ? "+" : ""}${r.expected_revenue_lift_30d}₴/30д)`,
        )
        .join("\n");
      await supabase.from("ai_insights").insert({
        insight_type: "pricing_recommendation",
        title: `💰 Pricing — ${top.length} рекомендацій (${totalLift >= 0 ? "+" : ""}${totalLift.toLocaleString("uk-UA")}₴/30д)`,
        description: `Аналіз price elasticity на основі акцій за 60 днів:\n\n${lines}`,
        confidence: 0.7,
        affected_layer: "pricing",
        risk_level: Math.abs(totalLift) > 5000 ? "medium" : "low",
        status: "new",
        expected_impact: `+${Math.abs(totalLift).toLocaleString("uk-UA")}₴/місяць`,
        metrics: { recommendations: top, total_lift_30d: totalLift },
      });
    }

    return new Response(
      JSON.stringify({
        scanned: products.length,
        recommendations: recommendations.length,
        top_5: top.slice(0, 5),
        total_lift_30d: top.reduce((s, r) => s + r.expected_revenue_lift_30d, 0),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
