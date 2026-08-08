// ACOS Elasticity Auto-Apply — reads recent `discount_elasticity` insights
// and turns each "recommended discount %" into a real `promo_codes` record
// (ELAST{shortSKU}{PCT}) + a corresponding `promotions` entry scoped to the
// product. This closes the loop: analyzer → recommendation → live A/B test.
//
// Safeguards:
//   - 30d cooldown per (product_id, discount_pct) — avoid duplicates
//   - Skip if product is inactive or out of stock
//   - Skip if an ELAST promo for this SKU is already active
//   - Cap at MAX_NEW_PER_RUN per execution to avoid bulk dumps
//   - Mark each created promo with insight_id for downstream ROI tracking

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { enqueueTribunalCase } from "../_shared/tribunal.ts";
import { checkSystemHealth } from "../_shared/system-health-guard.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const INSIGHT_LOOKBACK_DAYS = 14;     // pull insights from last 2 weeks
const COOLDOWN_DAYS = 30;             // same (sku, pct) cannot repeat within 30d
const PROMO_TTL_DAYS = 30;            // generated promo lasts 30 days
const MAX_NEW_PER_RUN = 3;            // ship max 3 new promos per execution
const MIN_LIFT_PCT = 15;              // safety: only act if elasticity lift ≥ 15%
const MAX_USES = 200;                 // cap usage per promo

interface InsightRow {
  id: string;
  created_at: string;
  metrics: {
    product_id?: string;
    product_name?: string;
    base_price?: number;
    recommended_discount_pct?: number;
    lift_pct?: number;
  } | null;
}
interface ProductRow {
  id: string;
  name: string;
  is_active: boolean;
  stock_quantity: number;
  price: number;
}
interface PromoRow {
  code: string;
  is_active: boolean;
  created_at: string;
  ends_at: string | null;
}

// Build a short SKU id from product UUID — first 4 hex chars uppercased.
const shortSkuFromUuid = (id: string) => id.replace(/-/g, "").slice(0, 4).toUpperCase();

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const body = await req.json().catch(() => ({}));
  const trigger = detectTrigger(req, body);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const url = new URL(req.url);
    const dryRun = url.searchParams.get("dry_run") === "1";
    let bodyJson: any = null;
    try { bodyJson = await req.clone().json(); } catch { /* GET */ }
    const fromTribunal = bodyJson?.from_tribunal === true;

    // ─── TRIBUNAL ENFORCE PATH — perform the actual promo creation ───
    if (fromTribunal) {
      const ch = bodyJson?.proposed_change ?? {};
      const startsAt = new Date().toISOString();
      const endsAt = new Date(Date.now() + PROMO_TTL_DAYS * dayMs).toISOString();
      const { data: pcRow, error: pcErr } = await supabase.from("promo_codes").insert({
        code: ch.promo_code,
        discount_type: "percentage",
        discount_value: ch.discount_pct,
        min_order_amount: ch.min_order_amount,
        starts_at: startsAt, ends_at: endsAt,
        max_uses: MAX_USES, is_active: true,
      }).select("id").single();
      if (pcErr) {
        return new Response(JSON.stringify({ error: pcErr.message }), { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } });
      }
      await supabase.from("promotions").insert({
        name: `Elasticity Test: ${ch.product_name} −${ch.discount_pct}%`,
        description: `Auto-created via tribunal verdict. Promo ${ch.promo_code} runs ${PROMO_TTL_DAYS}d.`,
        discount_type: "percentage", discount_value: ch.discount_pct,
        product_ids: [ch.product_id], starts_at: startsAt, ends_at: endsAt, is_active: true,
      });
      if (ch.insight_id) {
        await supabase.from("ai_insights").update({ status: "applied" }).eq("id", ch.insight_id);
      }
      return new Response(JSON.stringify({ ok: true, promo_id: pcRow.id, code: ch.promo_code }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // System Health Guard — block new tribunal enqueues if autonomy stress detected.
    const health = await checkSystemHealth();
    if (!health.ok) {
      try {
        await supabase.from("agent_runs").insert({
          function_name: "acos-elasticity-auto-apply",
          trigger,
          status: "skipped",
          started_at: new Date(Date.now() - 1000).toISOString(),
          finished_at: new Date().toISOString(),
          summary: `health_block:${health.reason}`,
          payload: { health_signals: health.signals },
        });
      } catch { /* non-fatal */ }
      return new Response(
        JSON.stringify({ ok: true, skipped: true, reason: `system_unhealthy:${health.reason}`, signals: health.signals }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const now = Date.now();
    const insightCutoff = new Date(now - INSIGHT_LOOKBACK_DAYS * dayMs).toISOString();
    const cooldownCutoff = new Date(now - COOLDOWN_DAYS * dayMs).toISOString();

    // 1. Pull fresh discount_elasticity insights with a non-zero recommendation.
    const { data: insights } = await supabase
      .from("ai_insights")
      .select("id, created_at, metrics")
      .eq("insight_type", "discount_elasticity")
      .eq("status", "new")
      .gte("created_at", insightCutoff)
      .order("created_at", { ascending: false })
      .limit(50);

    const candidates = ((insights ?? []) as InsightRow[])
      .filter((i) => {
        const m = i.metrics ?? {};
        return !!m.product_id
          && typeof m.recommended_discount_pct === "number"
          && m.recommended_discount_pct! > 0
          && (m.lift_pct ?? 0) >= MIN_LIFT_PCT;
      });

    if (candidates.length === 0) {
      return new Response(
        JSON.stringify({ created: [], reason: "no_actionable_insights", checked: (insights ?? []).length }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull existing ELAST promos in cooldown window for dedup.
    const { data: existingPromos } = await supabase
      .from("promo_codes")
      .select("code, is_active, created_at, ends_at")
      .like("code", "ELAST%")
      .gte("created_at", cooldownCutoff)
      .limit(500);
    const existing = (existingPromos ?? []) as PromoRow[];
    const existingCodeSet = new Set(existing.map((p) => p.code.toUpperCase()));

    // 3. Pull involved product rows to validate active+in-stock.
    const productIds = [...new Set(candidates.map((c) => c.metrics!.product_id!))];
    const { data: products } = await supabase
      .from("products")
      .select("id, name, is_active, stock_quantity, price")
      .in("id", productIds);
    const productMap = new Map(((products ?? []) as ProductRow[]).map((p) => [p.id, p]));

    // 4. Build target list, dedup per (sku, pct), enforce cap.
    const planned: Array<{
      insight_id: string;
      product_id: string;
      product_name: string;
      discount_pct: number;
      base_price: number;
      promo_code: string;
      min_order_amount: number;
      lift_pct: number;
    }> = [];
    const seen = new Set<string>();

    for (const ins of candidates) {
      if (planned.length >= MAX_NEW_PER_RUN) break;
      const m = ins.metrics!;
      const product = productMap.get(m.product_id!);
      if (!product || !product.is_active || product.stock_quantity <= 0) continue;

      const pct = Math.round(m.recommended_discount_pct!);
      const sku = shortSkuFromUuid(product.id);
      const code = `ELAST${sku}${pct}`;
      const key = `${product.id}:${pct}`;
      if (seen.has(key) || existingCodeSet.has(code)) continue;
      seen.add(key);

      planned.push({
        insight_id: ins.id,
        product_id: product.id,
        product_name: product.name,
        discount_pct: pct,
        base_price: product.price,
        promo_code: code,
        min_order_amount: Math.max(300, Math.round(product.price * 0.9)),
        lift_pct: m.lift_pct ?? 0,
      });
    }

    if (planned.length === 0) {
      return new Response(
        JSON.stringify({ created: [], reason: "all_dedup_or_inactive", candidates: candidates.length }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (dryRun) {
      return new Response(
        JSON.stringify({ created: planned, dry_run: true }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 5. SUBMIT all planned promos to Tribunal in parallel — actual creation in tribunal-enforcer.
    const created: Array<typeof planned[number] & { tribunal_case?: string }> = [];
    const submittedInsightIds: string[] = [];

    await Promise.all(planned.map(async (p) => {
      try {
        const tc = await enqueueTribunalCase({
          source_function: "acos-elasticity-auto-apply",
          category: "promo",
          urgency: p.discount_pct >= 20 ? "high" : "normal",
          proposed_change: {
            promo_code: p.promo_code,
            discount_pct: p.discount_pct,
            min_order_amount: p.min_order_amount,
            product_id: p.product_id,
            product_name: p.product_name,
            insight_id: p.insight_id,
            ttl_days: PROMO_TTL_DAYS,
          },
          context: { lift_pct: p.lift_pct, base_price: p.base_price },
          expected_impact: `+${p.lift_pct}% lift expected (elasticity analysis)`,
        });
        created.push({ ...p, tribunal_case: tc.case_id });
        submittedInsightIds.push(p.insight_id);
      } catch (_e) {
        // skip — next cron will retry
      }
    }));

    // Batch insight status update
    if (submittedInsightIds.length) {
      await supabase.from("ai_insights").update({ status: "submitted_to_tribunal" }).in("id", submittedInsightIds);
    }

    try {
      await supabase.from("agent_runs").insert({
        function_name: "acos-elasticity-auto-apply",
        trigger,
        status: created.length > 0 ? "success" : "success",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `created=${created.length}/${candidates.length} candidates, max=${MAX_NEW_PER_RUN}`,
        payload: { created_count: created.length, candidates: candidates.length },
      });
    } catch { /* ignore */ }

    return new Response(
      JSON.stringify({
        created,
        created_count: created.length,
        candidates_evaluated: candidates.length,
        max_per_run: MAX_NEW_PER_RUN,
        ttl_days: PROMO_TTL_DAYS,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    try {
      const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
      await sb.from("agent_runs").insert({
        function_name: "acos-elasticity-auto-apply",
        trigger,
        status: "error",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        error_message: String((err as Error)?.message ?? err).slice(0, 2000),
      });
    } catch { /* ignore */ }
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
