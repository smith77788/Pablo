// ACOS Profit Simulator — pre-action ROI forecaster.
//
// For each new ai_insight, simulates expected revenue impact based on:
//   1. Historical ai_memory pattern (avg_impact, success rate)
//   2. Baseline metrics (current 7d revenue for affected entity)
//   3. Risk-adjusted projection: expected_revenue_delta = baseline * avg_impact% * confidence
//
// Writes simulation result into ai_insights.metrics.simulation = {
//   baseline_revenue_7d, projected_delta_uah, projected_delta_pct,
//   confidence, verdict: 'positive'|'neutral'|'negative'|'unknown'
// }
//
// If verdict='negative' AND projected_delta_uah < -100, marks insight status='blocked_by_simulator'.
// This pre-empts the auto-promote engine (cycle #5) from acting on losing bets.
//
// Idempotent: only processes insights without simulation key in metrics. Safe to schedule hourly.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const WINDOW_DAYS = 7;
const MAX_BATCH = 30;
const NEGATIVE_BLOCK_THRESHOLD_UAH = -100;

interface InsightRow {
  id: string;
  insight_type: string;
  affected_layer: string | null;
  confidence: number;
  metrics: Record<string, unknown>;
  status: string;
}

interface MemoryRow {
  pattern_key: string;
  agent: string;
  category: string;
  confidence: number;
  avg_impact: number;
  success_count: number;
  failure_count: number;
}

const layerToAgent = (layer: string | null, insightType: string): string => {
  const l = (layer || "").toLowerCase();
  const t = (insightType || "").toLowerCase();
  if (l === "seo" || t.includes("seo") || t.includes("content")) return "seo";
  if (l === "stability" || l === "checkout" || t.includes("rollback") || t.includes("friction")) return "stability";
  return "growth";
};

const insightToActionCategory = (insightType: string): string | null => {
  const t = insightType.toLowerCase();
  if (t.includes("seo") || t.includes("content")) return "seo_copy_update";
  if (t.includes("bestseller") || t.includes("badge") || t.includes("sale_signal")) return "product_badge_toggle";
  if (t.includes("bundle") || t.includes("affinity")) return "bundle_suggestion";
  if (t.includes("price") || t.includes("elasticity")) return "price_change";
  if (t.includes("promo")) return "promo_change";
  return null;
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
    // Pull recent insights without simulation
    const since = new Date(Date.now() - 3 * dayMs).toISOString();
    const { data: insightsRaw, error: insightsErr } = await supabase
      .from("ai_insights")
      .select("id, insight_type, affected_layer, confidence, metrics, status")
      .in("status", ["new", "auto_applied"])
      .gte("created_at", since)
      .order("created_at", { ascending: false })
      .limit(MAX_BATCH * 2);
    if (insightsErr) throw insightsErr;

    const insights = ((insightsRaw ?? []) as InsightRow[]).filter(i => {
      const m = (i.metrics ?? {}) as Record<string, unknown>;
      return !m.simulation;
    }).slice(0, MAX_BATCH);

    if (insights.length === 0) {
      return new Response(
        JSON.stringify({ ok: true, processed: 0, message: "No insights pending simulation" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Load memory + 7d baseline revenue (sum of order totals)
    const baselineFrom = new Date(Date.now() - WINDOW_DAYS * dayMs).toISOString();
    const [memoryRes, ordersRes] = await Promise.all([
      supabase
        .from("ai_memory")
        .select("pattern_key, agent, category, confidence, avg_impact, success_count, failure_count")
        .eq("is_active", true),
      supabase
        .from("orders")
        .select("total")
        .gte("created_at", baselineFrom)
        .eq("status", "delivered"),
    ]);
    if (memoryRes.error) throw memoryRes.error;

    const memory = (memoryRes.data ?? []) as MemoryRow[];
    const baselineRevenue = (ordersRes.data ?? []).reduce(
      (sum: number, o: { total: number }) => sum + (Number(o.total) || 0),
      0,
    );

    const results: Array<{
      insight_id: string;
      verdict: string;
      projected_delta_uah: number;
      blocked: boolean;
    }> = [];
    let blockedCount = 0;
    const updateOps: Array<{ id: string; payload: Record<string, unknown> }> = [];

    for (const insight of insights) {
      const agent = layerToAgent(insight.affected_layer, insight.insight_type);
      const category = insightToActionCategory(insight.insight_type);

      const memMatch = category
        ? memory.find(m => m.agent === agent && m.category === category)
        : null;

      let projectedDeltaPct = 0;
      let projectedDeltaUah = 0;
      let verdict: "positive" | "neutral" | "negative" | "unknown" = "unknown";
      let basis = "no_memory_data";

      if (memMatch && (memMatch.success_count + memMatch.failure_count) >= 3) {
        // Risk-adjusted: avg_impact% × pattern confidence × insight confidence
        const adjustedImpactPct = memMatch.avg_impact * memMatch.confidence * Number(insight.confidence ?? 0.5);
        projectedDeltaPct = Math.round(adjustedImpactPct * 10) / 10;
        projectedDeltaUah = Math.round((baselineRevenue * adjustedImpactPct) / 100);
        verdict = projectedDeltaPct > 1 ? "positive" : projectedDeltaPct < -1 ? "negative" : "neutral";
        basis = `memory:${memMatch.pattern_key}`;
      } else if (memMatch) {
        // Insufficient samples — neutral with low confidence projection
        verdict = "neutral";
        basis = `memory_low_samples:${memMatch.pattern_key}`;
      }

      const simulation = {
        baseline_revenue_7d: baselineRevenue,
        projected_delta_pct: projectedDeltaPct,
        projected_delta_uah: projectedDeltaUah,
        verdict,
        basis,
        memory_confidence: memMatch?.confidence ?? null,
        memory_avg_impact: memMatch?.avg_impact ?? null,
        memory_samples: memMatch ? memMatch.success_count + memMatch.failure_count : 0,
        simulated_at: new Date().toISOString(),
      };

      const newMetrics = { ...(insight.metrics ?? {}), simulation };
      const shouldBlock = verdict === "negative" && projectedDeltaUah < NEGATIVE_BLOCK_THRESHOLD_UAH;

      const updatePayload: Record<string, unknown> = { metrics: newMetrics };
      if (shouldBlock && insight.status === "new") {
        updatePayload.status = "blocked_by_simulator";
        blockedCount++;
      }

      updateOps.push({ id: insight.id, payload: updatePayload });

      results.push({
        insight_id: insight.id,
        verdict,
        projected_delta_uah: projectedDeltaUah,
        blocked: shouldBlock,
      });
    }

    // Batch parallel updates instead of sequential per-insight writes
    await Promise.all(updateOps.map(u => supabase.from("ai_insights").update(u.payload).eq("id", u.id)));

    return new Response(
      JSON.stringify({
        ok: true,
        processed: insights.length,
        baseline_revenue_7d: baselineRevenue,
        blocked_count: blockedCount,
        memory_patterns_loaded: memory.length,
        results: results.slice(0, 20),
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-profit-simulator error", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
