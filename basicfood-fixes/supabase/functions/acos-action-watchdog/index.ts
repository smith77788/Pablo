// ACOS Cycle #8 — Auto-Rollback Watchdog
// Monitors ai_actions with status='applied' (auto-executed by acos-auto-promote-engine).
// For each action ≥24h and ≤72h old:
//   1. Compute baseline_revenue (7d before applied_at) and current_revenue (since applied_at).
//   2. Normalize to per-day rate (baseline_per_day vs current_per_day).
//   3. If current < baseline * 0.85 (≥15% drop) AND we have ≥10 sessions in current window:
//      - Mark action as 'reverted', set reverted_reason='auto_rollback_metric_drop'
//      - Update ai_memory: increment failure_count, recompute confidence
//      - Insert ai_insight of type='auto_rollback_executed'
//   4. Skip actions already reverted or with insufficient data.
//
// Action types monitored: seo_copy_update, product_badge_toggle, bundle_suggestion.
// SEO rollback is delegated to acos-seo-rollback-monitor (which already handles overrides table).
// This watchdog focuses on the *learning loop* — updating memory so the system stops repeating bad patterns.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const MIN_AGE_HOURS = 24;     // need at least 1 day post-apply data
const MAX_AGE_HOURS = 72;     // don't re-evaluate stale actions
const BASELINE_DAYS = 7;
const DROP_THRESHOLD = 0.15;  // 15% relative drop
const MIN_CURRENT_SESSIONS = 10;

interface ActionRow {
  id: string;
  action_type: string;
  agent_id: string;
  target_entity: string | null;
  target_id: string | null;
  parameters: Record<string, unknown>;
  applied_at: string;
  source_insight_id: string | null;
}

interface MetricSnapshot {
  sessions: number;
  revenue: number;
  per_day_revenue: number;
  per_day_sessions: number;
}

const computeMetrics = async (
  supabase: any,
  pagePath: string | null,
  productId: string | null,
  fromIso: string,
  toIso: string,
): Promise<MetricSnapshot> => {
  const windowMs = new Date(toIso).getTime() - new Date(fromIso).getTime();
  const windowDays = Math.max(1, windowMs / DAY_MS);

  // Sessions = unique session_id with relevant page_view
  let sessQuery = supabase
    .from("events")
    .select("session_id")
    .eq("event_type", "page_viewed")
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .limit(10000);

  if (pagePath) sessQuery = sessQuery.eq("url", pagePath);
  if (productId) sessQuery = sessQuery.eq("product_id", productId);

  const { data: views } = await sessQuery;
  const sessSet = new Set<string>();
  for (const v of views ?? []) {
    if (v.session_id) sessSet.add(v.session_id as string);
  }
  const sessions = sessSet.size;

  // Revenue = sum of orders.total in window scoped to product (if product_id given) or all
  let revQuery = supabase
    .from("orders")
    .select("total, id")
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .neq("status", "cancelled")
    .limit(5000);

  const { data: orders } = await revQuery;
  let revenue = 0;
  const orderIds: string[] = [];
  for (const o of orders ?? []) {
    revenue += (o.total as number) || 0;
    orderIds.push(o.id as string);
  }

  // If product-scoped, filter revenue to orders containing that product
  if (productId && orderIds.length > 0) {
    const chunkSize = 200;
    const chunks: string[][] = [];
    for (let i = 0; i < orderIds.length; i += chunkSize) chunks.push(orderIds.slice(i, i + chunkSize));
    const chunkResults = await Promise.all(
      chunks.map((chunk) =>
        supabase.from("order_items").select("order_id, product_price, quantity").eq("product_id", productId).in("order_id", chunk)
      ),
    );
    let scopedRev = 0;
    for (const { data: items } of chunkResults) {
      for (const it of items ?? []) {
        scopedRev += ((it.product_price as number) || 0) * ((it.quantity as number) || 1);
      }
    }
    revenue = scopedRev;
  }

  return {
    sessions,
    revenue,
    per_day_revenue: revenue / windowDays,
    per_day_sessions: sessions / windowDays,
  };
};

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
    const oldestApplied = new Date(now - MAX_AGE_HOURS * HOUR_MS).toISOString();
    const newestApplied = new Date(now - MIN_AGE_HOURS * HOUR_MS).toISOString();

    const { data: actions, error } = await supabase
      .from("ai_actions")
      .select("id, action_type, agent_id, target_entity, target_id, parameters, applied_at, source_insight_id")
      .eq("status", "applied")
      .is("reverted_at", null)
      .gte("applied_at", oldestApplied)
      .lte("applied_at", newestApplied)
      .limit(50);

    if (error) throw error;

    // Pre-fetch all ai_memory records for action pattern keys in one batch.
    const patternKeys = [
      ...new Set(((actions ?? []) as ActionRow[]).map((a) => `${a.agent_id}:${a.action_type}`)),
    ];
    const { data: memoryRows } = await supabase
      .from("ai_memory")
      .select("id, pattern_key, success_count, failure_count, confidence, avg_impact")
      .in("pattern_key", patternKeys);
    const memoryMap = new Map(
      (memoryRows ?? []).map((m: any) => [m.pattern_key, m]),
    );

    const results: Array<Record<string, unknown>> = [];
    let revertedCount = 0;

    for (const act of (actions ?? []) as ActionRow[] ) {
      const params = act.parameters ?? {};
      const pagePath = typeof params.page_path === "string" ? params.page_path : null;
      const productId = typeof params.product_id === "string"
        ? params.product_id
        : (act.target_entity === "product" ? act.target_id : null);

      // Skip if we have no scope to measure
      if (!pagePath && !productId) {
        results.push({ id: act.id, action: "skip", reason: "no_measurable_scope" });
        continue;
      }

      const appliedAt = new Date(act.applied_at);
      const baselineFrom = new Date(appliedAt.getTime() - BASELINE_DAYS * DAY_MS);
      const [baseline, current] = await Promise.all([
        computeMetrics(supabase, pagePath, productId, baselineFrom.toISOString(), act.applied_at),
        computeMetrics(supabase, pagePath, productId, act.applied_at, new Date(now).toISOString()),
      ]);

      if (current.sessions < MIN_CURRENT_SESSIONS) {
        results.push({ id: act.id, action: "skip", reason: "insufficient_current_sessions", current_sessions: current.sessions });
        continue;
      }

      if (baseline.per_day_revenue <= 0) {
        results.push({ id: act.id, action: "skip", reason: "no_baseline_revenue" });
        continue;
      }

      const drop = (baseline.per_day_revenue - current.per_day_revenue) / baseline.per_day_revenue;

      if (drop < DROP_THRESHOLD) {
        results.push({
          id: act.id,
          action: "keep",
          baseline_per_day: baseline.per_day_revenue,
          current_per_day: current.per_day_revenue,
          drop_pct: drop,
        });
        continue;
      }

      // ROLLBACK: mark action reverted, update memory, log insight — all in parallel.
      const patternKey = `${act.agent_id}:${act.action_type}`;
      const mem = memoryMap.get(patternKey) ?? null;

      const revertWrites: Promise<unknown>[] = [
        supabase.from("ai_actions").update({
          status: "reverted",
          reverted_at: new Date().toISOString(),
          reverted_reason: "auto_rollback_metric_drop",
          actual_result: {
            baseline_per_day_revenue: baseline.per_day_revenue,
            current_per_day_revenue: current.per_day_revenue,
            drop_pct: drop,
            baseline_sessions: baseline.sessions,
            current_sessions: current.sessions,
            measured_at: new Date().toISOString(),
          },
          measured_at: new Date().toISOString(),
        }).eq("id", act.id),
        supabase.from("ai_insights").insert({
          insight_type: "auto_rollback_executed",
          affected_layer: "stability",
          risk_level: "medium",
          title: `Auto-rollback: ${act.action_type}`,
          description: `Watchdog відкотив авто-дію ${act.action_type} (агент ${act.agent_id}). Виручка впала з ${baseline.per_day_revenue.toFixed(0)} ₴/день до ${current.per_day_revenue.toFixed(0)} ₴/день (-${(drop * 100).toFixed(1)}%) за ${MIN_AGE_HOURS}+ годин після застосування. Memory pattern оновлено.`,
          confidence: 0.9,
          metrics: {
            action_id: act.id,
            action_type: act.action_type,
            agent_id: act.agent_id,
            target_entity: act.target_entity,
            target_id: act.target_id,
            page_path: pagePath,
            product_id: productId,
            baseline_per_day_revenue: baseline.per_day_revenue,
            current_per_day_revenue: current.per_day_revenue,
            baseline_sessions: baseline.sessions,
            current_sessions: current.sessions,
            drop_pct: drop,
            memory_updated: !!mem,
          },
          status: "new",
        }),
      ];

      if (mem) {
        const newFail = (mem.failure_count as number) + 1;
        const newSucc = mem.success_count as number;
        const totalObs = newFail + newSucc;
        const newConfidence = totalObs > 0 ? Math.max(0.1, newSucc / totalObs) : 0.5;
        revertWrites.push(
          supabase.from("ai_memory").update({
            failure_count: newFail,
            confidence: newConfidence,
            last_observed_at: new Date().toISOString(),
            is_active: newConfidence >= 0.4,
          }).eq("id", mem.id),
        );
      }

      await Promise.all(revertWrites);

      revertedCount++;
      results.push({
        id: act.id,
        action: "reverted",
        action_type: act.action_type,
        baseline_per_day: baseline.per_day_revenue,
        current_per_day: current.per_day_revenue,
        drop_pct: drop,
      });
    }

    return new Response(
      JSON.stringify({
        ok: true,
        checked: actions?.length ?? 0,
        reverted: revertedCount,
        results,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-action-watchdog error", err);
    return new Response(
      JSON.stringify({ ok: false, error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
