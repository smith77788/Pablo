// ACOS Meta-Prior Injector — closes the self-learning loop by overriding
// per-product `discount_elasticity` recommendations with historic category-level
// STRONG WIN priors when raw data is too sparse for confident decisions.
//
// Decision logic per product:
//   1. Load latest discount_elasticity insight (status=new) for the product.
//   2. Find a matching elasticity_meta_conclusion (same category × price_tier)
//      with recommendation='use_as_default'.
//   3. If raw insight has weak signal (lift<15% OR <50 views) BUT a meta-prior
//      exists with win_rate≥70% → emit a `meta_prior_recommendation` insight
//      that overrides the per-product analysis with the meta tier.
//   4. Mark the original raw insight as 'superseded' so auto-apply uses the
//      meta-backed one.
//
// This is what makes the system improve over time: A/B-test history → meta
// conclusions → priors → better fresh recommendations → more A/B tests.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const RAW_LOOKBACK_DAYS = 14;       // only consider fresh raw insights
const META_LOOKBACK_DAYS = 90;      // meta priors valid for 90 days
const WEAK_LIFT_THRESHOLD = 15;     // raw recommendation considered weak below this
const WEAK_VIEWS_THRESHOLD = 50;    // also weak if low traffic
const STRONG_META_WIN_RATE = 70;
const MAX_OVERRIDES_PER_RUN = 10;

const PRICE_TIERS: Array<{ label: string; min: number; max: number }> = [
  { label: "<150₴", min: 0, max: 149 },
  { label: "150-300₴", min: 150, max: 299 },
  { label: "300-500₴", min: 300, max: 499 },
  { label: "500-800₴", min: 500, max: 799 },
  { label: "800₴+", min: 800, max: Infinity },
];
const tierFor = (price: number) =>
  PRICE_TIERS.find((t) => price >= t.min && price <= t.max)?.label ?? "unknown";

interface RawElastInsight {
  id: string;
  created_at: string;
  metrics: {
    product_id?: string;
    product_name?: string;
    base_price?: number;
    recommended_discount_pct?: number;
    lift_pct?: number;
    total_views?: number;
  } | null;
}
interface MetaConclusion {
  id: string;
  metrics: {
    bucket_key?: string;
    category?: string;
    price_tier?: string;
    discount_pct?: number;
    win_rate_pct?: number;
    avg_realized_lift_pct?: number;
    tests?: number;
    recommendation?: "use_as_default" | "neutral" | "avoid";
  } | null;
}
interface ProductRow {
  id: string;
  name: string;
  price: number;
  categories: string[];
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
    const url = new URL(req.url);
    const dryRun = url.searchParams.get("dry_run") === "1";
    const now = Date.now();
    const rawCutoff = new Date(now - RAW_LOOKBACK_DAYS * dayMs).toISOString();
    const metaCutoff = new Date(now - META_LOOKBACK_DAYS * dayMs).toISOString();

    // 1. Pull fresh raw discount_elasticity insights still pending.
    const { data: rawInsights } = await supabase
      .from("ai_insights")
      .select("id, created_at, metrics")
      .eq("insight_type", "discount_elasticity")
      .eq("status", "new")
      .gte("created_at", rawCutoff)
      .order("created_at", { ascending: false })
      .limit(100);

    const rawList = ((rawInsights ?? []) as RawElastInsight[]).filter(
      (r) => !!r.metrics?.product_id,
    );

    if (rawList.length === 0) {
      return new Response(
        JSON.stringify({ overrides: [], reason: "no_pending_raw_insights", raw_evaluated: 0, meta_priors_available: 0 }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull active meta-conclusions (use_as_default) within validity window.
    const { data: metaInsights } = await supabase
      .from("ai_insights")
      .select("id, metrics")
      .eq("insight_type", "elasticity_meta_conclusion")
      .gte("created_at", metaCutoff)
      .limit(500);

    const metaPriors = ((metaInsights ?? []) as MetaConclusion[]).filter((m) => {
      const x = m.metrics ?? {};
      return x.recommendation === "use_as_default"
        && (x.win_rate_pct ?? 0) >= STRONG_META_WIN_RATE
        && !!x.category && !!x.price_tier && typeof x.discount_pct === "number";
    });

    if (metaPriors.length === 0) {
      return new Response(
        JSON.stringify({
          overrides: [],
          raw_evaluated: rawList.length,
          meta_priors_available: 0,
          reason: "no_strong_meta_priors_available",
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 3. Index meta priors by (category, price_tier) for fast lookup. If
    //    multiple discount_pct values match → keep the one with highest
    //    win_rate × avg_realized_lift.
    const priorIndex = new Map<string, MetaConclusion>();
    for (const m of metaPriors) {
      const key = `${m.metrics!.category}|${m.metrics!.price_tier}`;
      const existing = priorIndex.get(key);
      const score = (m.metrics!.win_rate_pct ?? 0) * Math.max(0, m.metrics!.avg_realized_lift_pct ?? 0);
      const existingScore = existing
        ? (existing.metrics!.win_rate_pct ?? 0) * Math.max(0, existing.metrics!.avg_realized_lift_pct ?? 0)
        : -1;
      if (score > existingScore) priorIndex.set(key, m);
    }

    // 4. Pull involved products to get categories + price.
    const productIds = [...new Set(rawList.map((r) => r.metrics!.product_id!))];
    const { data: products } = await supabase
      .from("products")
      .select("id, name, price, categories")
      .in("id", productIds);
    const productMap = new Map(((products ?? []) as ProductRow[]).map((p) => [p.id, p]));

    // 5. For each raw insight, decide if a meta-prior should override it.
    const overrides: Array<{
      raw_insight_id: string;
      meta_insight_id: string;
      product_id: string;
      product_name: string;
      original_pct: number;
      original_lift: number;
      meta_pct: number;
      meta_win_rate: number;
      meta_avg_lift: number;
      reason: "weak_lift" | "low_traffic" | "weak_lift_and_low_traffic";
    }> = [];

    for (const raw of rawList) {
      if (overrides.length >= MAX_OVERRIDES_PER_RUN) break;
      const m = raw.metrics!;
      const product = productMap.get(m.product_id!);
      if (!product) continue;

      const tier = tierFor(product.price);
      const cats = product.categories?.length ? product.categories : ["uncategorized"];

      // Find best matching meta prior across all product's categories.
      let bestPrior: MetaConclusion | null = null;
      let bestPriorCat = "";
      for (const cat of cats) {
        const candidate = priorIndex.get(`${cat}|${tier}`);
        if (!candidate) continue;
        const cs = (candidate.metrics!.win_rate_pct ?? 0) * Math.max(0, candidate.metrics!.avg_realized_lift_pct ?? 0);
        const bs = bestPrior
          ? (bestPrior.metrics!.win_rate_pct ?? 0) * Math.max(0, bestPrior.metrics!.avg_realized_lift_pct ?? 0)
          : -1;
        if (cs > bs) {
          bestPrior = candidate;
          bestPriorCat = cat;
        }
      }
      if (!bestPrior) continue;

      const lowLift = (m.lift_pct ?? 0) < WEAK_LIFT_THRESHOLD;
      const lowTraffic = (m.total_views ?? 0) < WEAK_VIEWS_THRESHOLD;
      if (!lowLift && !lowTraffic) continue;

      // Only override if the meta tier differs from raw recommendation
      // OR raw recommendation was 0 (no-discount) but meta says discount wins.
      const rawPct = m.recommended_discount_pct ?? 0;
      const metaPct = bestPrior.metrics!.discount_pct!;
      if (rawPct === metaPct) continue;

      overrides.push({
        raw_insight_id: raw.id,
        meta_insight_id: bestPrior.id,
        product_id: product.id,
        product_name: product.name,
        original_pct: rawPct,
        original_lift: m.lift_pct ?? 0,
        meta_pct: metaPct,
        meta_win_rate: bestPrior.metrics!.win_rate_pct ?? 0,
        meta_avg_lift: bestPrior.metrics!.avg_realized_lift_pct ?? 0,
        reason: lowLift && lowTraffic ? "weak_lift_and_low_traffic" : lowLift ? "weak_lift" : "low_traffic",
      });

      void bestPriorCat; // included in payload via meta insight reference
    }

    if (overrides.length === 0) {
      return new Response(
        JSON.stringify({
          overrides: [],
          raw_evaluated: rawList.length,
          meta_priors_available: priorIndex.size,
          reason: "no_weak_raw_with_matching_prior",
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (dryRun) {
      return new Response(
        JSON.stringify({ overrides, dry_run: true }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 6. Build all insight rows, batch-insert, then batch-supersede raw ones.
    const insightRows = overrides.map((o) => {
      const product = productMap.get(o.product_id)!;
      const tier = tierFor(product.price);
      const expectedLift = Math.round(o.meta_avg_lift);
      return {
        insight_type: "discount_elasticity",
        title: `🧠 META prior: ${o.product_name} → −${o.meta_pct}% (від ${o.meta_win_rate}% historic win-rate)`,
        description: `Raw discount-elasticity для "${o.product_name}" мав слабкий сигнал (${o.reason === "weak_lift" ? `lift ${o.original_lift}%<${WEAK_LIFT_THRESHOLD}%` : o.reason === "low_traffic" ? `<${WEAK_VIEWS_THRESHOLD} views` : `lift ${o.original_lift}%, low traffic`}). Замість per-product даних застосовуємо historic meta-prior: для категорії "${product.categories?.[0] ?? "—"}" у tier ${tier} знижка ${o.meta_pct}% виграла ${o.meta_win_rate}% попередніх A/B-тестів з середнім realized lift +${o.meta_avg_lift}%. Original raw insight superseded.`,
        expected_impact: `+${expectedLift}% expected AOV lift на основі ${o.meta_win_rate}% historic win-rate`,
        confidence: Math.min(0.9, 0.5 + (o.meta_win_rate / 200)),
        risk_level: "low",
        affected_layer: "merchandising",
        status: "new",
        metrics: {
          product_id: o.product_id,
          product_name: o.product_name,
          base_price: product.price,
          recommended_discount_pct: o.meta_pct,
          lift_pct: expectedLift,
          source: "meta_prior",
          source_meta_insight_id: o.meta_insight_id,
          superseded_raw_insight_id: o.raw_insight_id,
          original_raw_pct: o.original_pct,
          original_raw_lift: o.original_lift,
          override_reason: o.reason,
        },
      };
    });

    const created: typeof overrides = [];
    if (insightRows.length > 0) {
      const { error: insErr } = await supabase.from("ai_insights").insert(insightRows);
      if (!insErr) {
        created.push(...overrides);
        // Batch-supersede all raw insights whose meta-prior was just created
        const rawIds = overrides.map((o) => o.raw_insight_id).filter(Boolean);
        if (rawIds.length > 0) {
          await supabase.from("ai_insights").update({ status: "superseded" }).in("id", rawIds);
        }
      }
    }

    return new Response(
      JSON.stringify({
        overrides_created: created.length,
        raw_evaluated: rawList.length,
        meta_priors_available: priorIndex.size,
        max_per_run: MAX_OVERRIDES_PER_RUN,
        details: created,
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
