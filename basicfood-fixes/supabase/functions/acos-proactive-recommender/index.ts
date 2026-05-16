/**
 * ACOS Proactive Recommender (adaptive)
 *
 * Closes the learning loop by acting on what `ai_memory` has *proven* works.
 *
 * Two recommendation tracks:
 *   1. PROVEN — well-established winners (≥MIN_SUCCESS, ≥MIN_SUCCESS_RATE,
 *      ≥MIN_AVG_IMPACT) that are under-utilised in the last 14 days.
 *   2. EMERGING — early winners (≥MIN_EMERGING_SUCCESS with high impact)
 *      surfaced so the system isn't silent during bootstrap. These get
 *      lower confidence and `risk_level=medium`.
 *
 * Adaptive thresholds: when the system has very few proven patterns (<5 active
 * with success_count≥3), MIN_SUCCESS temporarily drops to MIN_SUCCESS_BOOT so
 * the loop produces signal during early data collection.
 *
 * Idempotent via dedup_bucket (one rec per pattern per day).
 *
 * Runs daily.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const RECENT_WINDOW_MS = 14 * 24 * 60 * 60 * 1000;

// Steady-state thresholds
const MIN_SUCCESS = 5;
const MIN_SUCCESS_RATE = 0.7;
const MIN_AVG_IMPACT = 5; // percent

// Bootstrap mode (when system has few mature patterns)
const MIN_SUCCESS_BOOT = 2;
const BOOTSTRAP_TRIGGER_PROVEN_COUNT = 5; // <5 patterns w/ success≥3 → boot mode

// Emerging-pattern track
const MIN_EMERGING_SUCCESS = 1;
const MIN_EMERGING_IMPACT = 10; // percent — must be a real signal, not noise

interface MemoryRow {
  id: string;
  pattern_key: string;
  agent: string;
  category: string;
  learned_rule: string;
  success_count: number;
  failure_count: number;
  avg_impact: number;
  confidence: number;
  is_active: boolean;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const body = await req.json().catch(() => ({}));
  const trigger = detectTrigger(req, body);

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    // 0. Decide adaptive threshold
    const { count: matureCount } = await sb
      .from("ai_memory")
      .select("*", { count: "exact", head: true })
      .eq("is_active", true)
      .gte("success_count", 3);
    const bootstrapMode = (matureCount ?? 0) < BOOTSTRAP_TRIGGER_PROVEN_COUNT;
    const effectiveMinSuccess = bootstrapMode ? MIN_SUCCESS_BOOT : MIN_SUCCESS;

    // 1. Pull all candidate active patterns with at least 1 success and positive impact
    const { data: patterns, error: pErr } = await sb
      .from("ai_memory")
      .select("id, pattern_key, agent, category, learned_rule, success_count, failure_count, avg_impact, confidence, is_active")
      .eq("is_active", true)
      .gte("success_count", MIN_EMERGING_SUCCESS)
      .gt("avg_impact", 0)
      .order("avg_impact", { ascending: false })
      .limit(200);
    if (pErr) throw pErr;

    const all = (patterns ?? []) as MemoryRow[];

    const proven = all.filter((p) => {
      const total = p.success_count + p.failure_count;
      const rate = total > 0 ? p.success_count / total : 0;
      return p.success_count >= effectiveMinSuccess
        && rate >= MIN_SUCCESS_RATE
        && p.avg_impact >= MIN_AVG_IMPACT;
    });

    const provenIds = new Set(proven.map((p) => p.id));
    const emerging = all.filter((p) => {
      if (provenIds.has(p.id)) return false;
      const total = p.success_count + p.failure_count;
      const rate = total > 0 ? p.success_count / total : 0;
      return p.success_count >= MIN_EMERGING_SUCCESS
        && p.avg_impact >= MIN_EMERGING_IMPACT
        && rate >= 0.5;
    });

    // 2. For each candidate, count recent uses for the same agent
    const since = new Date(Date.now() - RECENT_WINDOW_MS).toISOString();
    const dedupBucket = Math.floor(Date.now() / (24 * 60 * 60 * 1000));
    let inserted = 0;
    const recommendations: Array<{ pattern: string; track: string; recent_uses: number }> = [];

    async function emit(p: MemoryRow, track: "proven" | "emerging") {
      const { count: recentUses } = await sb
        .from("ai_actions")
        .select("*", { count: "exact", head: true })
        .eq("agent_id", p.agent)
        .in("status", ["applied", "measured"])
        .gte("created_at", since);
      const uses = recentUses ?? 0;

      // Under-utilisation:
      //  - proven: uses < success_count (i.e. we used the winner less than its history suggests)
      //  - emerging: any pattern with <2 uses in 14d is worth surfacing
      const threshold = track === "proven" ? Math.max(1, p.success_count) : 2;
      if (uses >= threshold) return;

      const total = p.success_count + p.failure_count;
      const successRate = total > 0 ? p.success_count / total : 0;

      const titlePrefix = track === "proven" ? "Proven pattern under-used" : "Emerging winner — worth replicating";
      const title = `${titlePrefix}: ${p.pattern_key}`;
      const description = track === "proven"
        ? `Pattern "${p.learned_rule}" has ${p.success_count} successes vs ${p.failure_count} failures `
          + `(${(successRate * 100).toFixed(0)}% success, avg impact +${p.avg_impact.toFixed(1)}%) `
          + `but was applied only ${uses} time(s) in 14d. Consider running ${p.agent} more often for ${p.category}.`
        : `Early signal: "${p.learned_rule}" — ${p.success_count} success(es), avg impact +${p.avg_impact.toFixed(1)}%. `
          + `Used ${uses}× in 14d. Worth running ${p.agent} again to confirm the pattern (n is still small).`;

      const { data: existing } = await sb
        .from("ai_insights")
        .select("id")
        .eq("insight_type", "proactive_recommendation")
        .eq("dedup_bucket", dedupBucket)
        .ilike("title", `%${p.pattern_key}%`)
        .maybeSingle();
      if (existing) return;

      const { error: insErr } = await sb.from("ai_insights").insert({
        insight_type: "proactive_recommendation",
        title,
        description,
        confidence: track === "proven"
          ? Math.max(0.6, Math.min(0.95, p.confidence))
          : Math.max(0.4, Math.min(0.7, p.confidence)),
        risk_level: track === "proven" ? "low" : "medium",
        affected_layer: p.category,
        expected_impact: `Replicating may add ~${p.avg_impact.toFixed(1)}% lift`,
        metrics: {
          pattern_key: p.pattern_key,
          agent: p.agent,
          category: p.category,
          track,
          bootstrap_mode: bootstrapMode,
          success_count: p.success_count,
          failure_count: p.failure_count,
          success_rate: Math.round(successRate * 100) / 100,
          avg_impact_pct: p.avg_impact,
          recent_uses_14d: uses,
        },
        dedup_bucket: dedupBucket,
        status: "open",
      });
      if (insErr) {
        console.warn("[proactive-recommender] insert failed:", insErr.message);
        return;
      }
      inserted++;
      recommendations.push({ pattern: p.pattern_key, track, recent_uses: uses });
    }

    await Promise.all([
      ...proven.map((p) => emit(p, "proven")),
      ...emerging.map((p) => emit(p, "emerging")),
    ]);

    try {
      await sb.from("agent_runs").insert({
        function_name: "acos-proactive-recommender",
        trigger,
        status: "success",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `mode=${bootstrapMode ? "bootstrap" : "steady"}, proven=${proven.length}, emerging=${emerging.length}, recs=${inserted}`,
        payload: { recommendations, bootstrapMode, effectiveMinSuccess },
      });
    } catch { /* non-fatal */ }

    return new Response(
      JSON.stringify({
        ok: true,
        bootstrap_mode: bootstrapMode,
        effective_min_success: effectiveMinSuccess,
        proven_patterns: proven.length,
        emerging_patterns: emerging.length,
        recommendations_inserted: inserted,
        recommendations,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-proactive-recommender error", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
