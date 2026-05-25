// ACOS Cross-Agent Conflict Resolver — detects conflicting hypotheses across agents
// and arbitrates by expected revenue delta from the profit simulator.
//
// Conflict types detected:
//   1. Same target_entity + opposite direction (e.g., SEO wants premium-positioning,
//      Pricing wants discount on the same product)
//   2. Same page_path with multiple competing SEO experiments
//   3. Same product appearing in both bundle_suggestion AND price_change
//
// Resolution:
//   - Group insights by conflict_key (target_entity + target_id)
//   - Within each group, pick the winner: highest projected_delta_uah from simulation
//   - Mark losers as status='blocked_by_conflict' with conflict_with metadata
//   - If no simulator data → fallback to highest confidence × insight count
//
// Runs every 30 minutes. Idempotent — only processes status='new' insights.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const LOOKBACK_DAYS = 2;
const MAX_BATCH = 100;

interface InsightRow {
  id: string;
  insight_type: string;
  affected_layer: string | null;
  confidence: number;
  metrics: Record<string, unknown>;
  status: string;
  title: string;
}

const layerToAgent = (layer: string | null, insightType: string): string => {
  const l = (layer || "").toLowerCase();
  const t = (insightType || "").toLowerCase();
  if (l === "seo" || t.includes("seo") || t.includes("content")) return "seo";
  if (l === "stability" || l === "checkout" || t.includes("rollback") || t.includes("friction")) return "stability";
  if (l === "pricing" || t.includes("price") || t.includes("elasticity")) return "pricing";
  if (l === "promo" || t.includes("promo") || t.includes("discount")) return "promo";
  if (t.includes("bundle") || t.includes("affinity")) return "bundle";
  return "growth";
};

// Conflict key: groups insights that touch the same target.
// Format: "{layer}:{type_family}" — pricing:product, seo:page_path, etc.
const conflictKey = (insight: InsightRow): string | null => {
  const m = (insight.metrics ?? {}) as Record<string, unknown>;
  const layer = (insight.affected_layer ?? "").toLowerCase();
  const t = insight.insight_type.toLowerCase();

  // SEO conflicts: same page_path
  const pagePath = typeof m.page_path === "string" ? m.page_path : null;
  if (pagePath && (layer === "seo" || t.includes("seo"))) {
    return `seo:page:${pagePath}`;
  }

  // Product-level conflicts: same product_id touched by competing agents
  const productId = typeof m.product_id === "string"
    ? m.product_id
    : typeof m.target_product_id === "string"
    ? m.target_product_id
    : null;
  if (productId) {
    return `product:${productId}`;
  }

  // Promo/pricing conflicts at category level
  const category = typeof m.category === "string" ? m.category : null;
  if (category && (t.includes("promo") || t.includes("price"))) {
    return `category_promo:${category}`;
  }

  return null;
};

interface SimResult {
  verdict?: string;
  projected_delta_uah?: number;
}

const getProjectedDelta = (insight: InsightRow): number => {
  const sim = (insight.metrics as Record<string, unknown>)?.simulation as SimResult | undefined;
  if (sim && typeof sim.projected_delta_uah === "number") return sim.projected_delta_uah;
  return 0;
};

const getFallbackScore = (insight: InsightRow): number => {
  // No simulation → use confidence as proxy
  return Number(insight.confidence ?? 0.5) * 1000;
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
    const since = new Date(Date.now() - LOOKBACK_DAYS * dayMs).toISOString();
    const { data: insightsRaw, error } = await supabase
      .from("ai_insights")
      .select("id, insight_type, affected_layer, confidence, metrics, status, title")
      .eq("status", "new")
      .gte("created_at", since)
      .limit(MAX_BATCH);
    if (error) throw error;

    const insights = (insightsRaw ?? []) as InsightRow[];
    if (insights.length === 0) {
      return new Response(
        JSON.stringify({ ok: true, processed: 0, message: "No new insights to arbitrate" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Group by conflict key
    const groups = new Map<string, InsightRow[]>();
    let ungrouped = 0;
    for (const insight of insights) {
      const key = conflictKey(insight);
      if (!key) {
        ungrouped++;
        continue;
      }
      const arr = groups.get(key) ?? [];
      arr.push(insight);
      groups.set(key, arr);
    }

    let conflictsFound = 0;
    let losersBlocked = 0;
    const resolutions: Array<{
      conflict_key: string;
      participants: number;
      winner_id: string;
      winner_agent: string;
      winner_delta: number;
      losers: Array<{ id: string; agent: string; delta: number }>;
    }> = [];
    const insightUpdateOps: Array<{ id: string; payload: Record<string, unknown> }> = [];

    for (const [key, group] of groups.entries()) {
      if (group.length < 2) continue;
      conflictsFound++;

      // Score each: prefer profit simulator delta; fall back to confidence
      const scored = group.map(g => ({
        insight: g,
        agent: layerToAgent(g.affected_layer, g.insight_type),
        delta: getProjectedDelta(g),
        fallback: getFallbackScore(g),
      }));

      // Winner: highest delta; if all zero, highest confidence
      const allZero = scored.every(s => s.delta === 0);
      scored.sort((a, b) => allZero ? b.fallback - a.fallback : b.delta - a.delta);

      const [winner, ...losers] = scored;
      const conflictMeta = {
        conflict_key: key,
        winner_id: winner.insight.id,
        winner_agent: winner.agent,
        winner_delta_uah: winner.delta,
        decided_at: new Date().toISOString(),
        decision_basis: allZero ? "confidence_fallback" : "projected_revenue",
      };

      // Collect winner update
      insightUpdateOps.push({
        id: winner.insight.id,
        payload: {
          metrics: {
            ...(winner.insight.metrics ?? {}),
            conflict_resolution: { ...conflictMeta, role: "winner", competitors: losers.length },
          },
        },
      });

      // Collect loser updates
      for (const loser of losers) {
        insightUpdateOps.push({
          id: loser.insight.id,
          payload: {
            status: "blocked_by_conflict",
            metrics: {
              ...(loser.insight.metrics ?? {}),
              conflict_resolution: {
                ...conflictMeta,
                role: "loser",
                loser_agent: loser.agent,
                loser_delta_uah: loser.delta,
                blocked_in_favor_of: winner.insight.id,
              },
            },
          },
        });
        losersBlocked++;
      }

      resolutions.push({
        conflict_key: key,
        participants: group.length,
        winner_id: winner.insight.id,
        winner_agent: winner.agent,
        winner_delta: winner.delta,
        losers: losers.map(l => ({ id: l.insight.id, agent: l.agent, delta: l.delta })),
      });
    }

    // Batch parallel updates instead of sequential per-insight writes
    if (insightUpdateOps.length > 0) {
      await Promise.all(
        insightUpdateOps.map(u => supabase.from("ai_insights").update(u.payload).eq("id", u.id))
      );
    }

    return new Response(
      JSON.stringify({
        ok: true,
        processed: insights.length,
        ungrouped,
        groups_total: groups.size,
        conflicts_found: conflictsFound,
        losers_blocked: losersBlocked,
        resolutions: resolutions.slice(0, 20),
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-conflict-resolver error", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
