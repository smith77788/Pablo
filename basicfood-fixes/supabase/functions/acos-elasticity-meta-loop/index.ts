// ACOS Elasticity Meta-Loop — monthly meta-analysis of all ELAST ROI verdicts
// Groups historic ELAST tracking insights by (category, price_tier, discount_pct)
// to surface category-level optimal discount conclusions like:
//   "For category 'jerky' at price 200-400₴, 10% discount wins 80% of A/B tests"
//
// Output: emits `elasticity_meta_conclusion` insights — one per (category, tier)
// combo with ≥3 tests, ranked by win-rate × avg-realized-lift. These conclusions
// guide future acos-discount-elasticity recommendations and inform manual
// merchandising decisions.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const LOOKBACK_DAYS = 180;          // 6-month historical window
const MIN_TESTS_PER_GROUP = 3;      // need at least 3 evaluated tests per bucket
const PRICE_TIERS: Array<{ label: string; min: number; max: number }> = [
  { label: "<150₴", min: 0, max: 149 },
  { label: "150-300₴", min: 150, max: 299 },
  { label: "300-500₴", min: 300, max: 499 },
  { label: "500-800₴", min: 500, max: 799 },
  { label: "800₴+", min: 800, max: Infinity },
];

interface ElastTrackingMetrics {
  product_id?: string;
  product_name?: string;
  discount_pct?: number;
  expected_lift_pct?: number;
  roi_verdict?: "winner" | "flop";
  roi_realized_lift_pct?: number;
  roi_uses?: number;
  roi_net_revenue?: number;
}
interface InsightRow {
  id: string;
  status: string;
  metrics: ElastTrackingMetrics | null;
}
interface ProductRow {
  id: string;
  name: string;
  price: number;
  categories: string[];
}

const tierFor = (price: number) =>
  PRICE_TIERS.find((t) => price >= t.min && price <= t.max)?.label ?? "unknown";

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
    const cutoff = new Date(now - LOOKBACK_DAYS * dayMs).toISOString();

    // 1. Pull all ELAST tracking insights with a final verdict.
    const { data: insights } = await supabase
      .from("ai_insights")
      .select("id, status, metrics")
      .eq("insight_type", "elasticity_auto_applied")
      .in("status", ["validated", "rejected"])
      .gte("created_at", cutoff)
      .limit(1000);

    const decided = ((insights ?? []) as InsightRow[]).filter((i) => {
      const m = i.metrics ?? {};
      return !!m.product_id
        && typeof m.discount_pct === "number"
        && (m.roi_verdict === "winner" || m.roi_verdict === "flop");
    });

    if (decided.length === 0) {
      return new Response(
        JSON.stringify({ analyzed: 0, conclusions: 0, reason: "no_decided_tests_in_window" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull involved product info to get categories + price.
    const productIds = [...new Set(decided.map((d) => d.metrics!.product_id!))];
    const { data: products } = await supabase
      .from("products")
      .select("id, name, price, categories")
      .in("id", productIds);
    const productMap = new Map(((products ?? []) as ProductRow[]).map((p) => [p.id, p]));

    // 3. Bucket each test into (category, price_tier, discount_pct).
    interface Bucket {
      category: string;
      price_tier: string;
      discount_pct: number;
      tests: number;
      winners: number;
      flops: number;
      avg_realized_lift_pct: number;
      total_net_revenue: number;
      sample_products: string[];
    }
    const buckets = new Map<string, Bucket>();

    for (const ins of decided) {
      const m = ins.metrics!;
      const product = productMap.get(m.product_id!);
      if (!product) continue;
      const tier = tierFor(product.price);
      const cats = product.categories?.length ? product.categories : ["uncategorized"];

      // A product can belong to multiple categories — count it in each.
      for (const cat of cats) {
        const key = `${cat}|${tier}|${m.discount_pct}`;
        let b = buckets.get(key);
        if (!b) {
          b = {
            category: cat,
            price_tier: tier,
            discount_pct: m.discount_pct!,
            tests: 0,
            winners: 0,
            flops: 0,
            avg_realized_lift_pct: 0,
            total_net_revenue: 0,
            sample_products: [],
          };
          buckets.set(key, b);
        }
        b.tests += 1;
        if (m.roi_verdict === "winner") b.winners += 1;
        else b.flops += 1;
        b.avg_realized_lift_pct += m.roi_realized_lift_pct ?? 0;
        b.total_net_revenue += m.roi_net_revenue ?? 0;
        if (b.sample_products.length < 3 && !b.sample_products.includes(product.name)) {
          b.sample_products.push(product.name);
        }
      }
    }

    // 4. Filter to actionable buckets, finalize averages.
    const actionable = [...buckets.values()]
      .filter((b) => b.tests >= MIN_TESTS_PER_GROUP)
      .map((b) => ({
        ...b,
        avg_realized_lift_pct: Math.round(b.avg_realized_lift_pct / b.tests),
        win_rate_pct: Math.round((b.winners / b.tests) * 100),
      }))
      .sort(
        (a, b) =>
          b.win_rate_pct * Math.max(0, b.avg_realized_lift_pct) -
          a.win_rate_pct * Math.max(0, a.avg_realized_lift_pct),
      );

    if (actionable.length === 0) {
      return new Response(
        JSON.stringify({
          analyzed: decided.length,
          conclusions: 0,
          reason: "no_buckets_meet_min_tests",
          min_tests: MIN_TESTS_PER_GROUP,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 5. Dedup: skip if a meta-conclusion for this exact (cat, tier, pct) was
    //    already created within the last 30 days.
    const dedupCutoff = new Date(now - 30 * dayMs).toISOString();
    const { data: recentMeta } = await supabase
      .from("ai_insights")
      .select("metrics")
      .eq("insight_type", "elasticity_meta_conclusion")
      .gte("created_at", dedupCutoff)
      .limit(500);
    const existingKeys = new Set(
      ((recentMeta ?? []) as Array<{ metrics: { bucket_key?: string } | null }>).map(
        (r) => r.metrics?.bucket_key,
      ).filter(Boolean),
    );

    const created: typeof actionable = [];
    const insightRows: object[] = [];
    for (const b of actionable) {
      const bucketKey = `${b.category}|${b.price_tier}|${b.discount_pct}`;
      if (existingKeys.has(bucketKey)) continue;

      const verdict =
        b.win_rate_pct >= 70 && b.avg_realized_lift_pct >= 10
          ? "✅ STRONG WIN"
          : b.win_rate_pct >= 50
          ? "⚖️ NEUTRAL"
          : "❌ AVOID";

      insightRows.push({
        insight_type: "elasticity_meta_conclusion",
        title: `${verdict}: ${b.category} ${b.price_tier} −${b.discount_pct}% (${b.win_rate_pct}% win)`,
        description: `За останні ${LOOKBACK_DAYS}д ELAST A/B-тестів для категорії "${b.category}" у ціновому tier ${b.price_tier} зі знижкою ${b.discount_pct}%: ${b.tests} тест(ів), ${b.winners} winners / ${b.flops} flops (${b.win_rate_pct}% win-rate), середній realized lift +${b.avg_realized_lift_pct}%, ${b.total_net_revenue.toLocaleString()}₴ загального net revenue. Зразки товарів: ${b.sample_products.join(", ")}.`,
        expected_impact:
          b.win_rate_pct >= 70
            ? `Застосовуй ${b.discount_pct}% за замовчуванням для нових товарів цієї категорії/tier — historic baseline`
            : b.win_rate_pct < 30
            ? `Уникай ${b.discount_pct}% для цієї категорії/tier — historic flop`
            : "Тримай як option, але тестуй інші tiers",
        confidence: Math.min(0.95, 0.5 + (b.tests / 20) + (b.win_rate_pct / 200)),
        risk_level: "low",
        affected_layer: "merchandising",
        status: "new",
        metrics: {
          bucket_key: bucketKey,
          category: b.category,
          price_tier: b.price_tier,
          discount_pct: b.discount_pct,
          tests: b.tests,
          winners: b.winners,
          flops: b.flops,
          win_rate_pct: b.win_rate_pct,
          avg_realized_lift_pct: b.avg_realized_lift_pct,
          total_net_revenue: b.total_net_revenue,
          sample_products: b.sample_products,
          recommendation:
            b.win_rate_pct >= 70 && b.avg_realized_lift_pct >= 10
              ? "use_as_default"
              : b.win_rate_pct < 30
              ? "avoid"
              : "neutral",
        },
      });
      created.push(b);
    }
    if (insightRows.length > 0) {
      await supabase.from("ai_insights").insert(insightRows);
    }

    return new Response(
      JSON.stringify({
        analyzed: decided.length,
        buckets_evaluated: buckets.size,
        actionable: actionable.length,
        conclusions_created: created.length,
        top_conclusions: created.slice(0, 5),
        all_actionable: actionable,
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
