/**
 * ACOS Recommendation Actuator
 *
 * Закриває розрив "рекомендація → дія":
 * читає `ai_insights` з insight_type='proactive_recommendation' status='open'
 * і автоматично створює `tribunal_cases` для повторного прогону патерна.
 *
 * Логіка:
 *   - proven track  → urgency=normal, ставимо в стандартну чергу
 *   - emerging track → urgency=low, нижчий пріоритет (ще треба верифікувати)
 *
 * Безпека:
 *   - Rate-limit: ≤5 actuations за 24h (захист від нескінченного циклу)
 *   - Idempotency: skip якщо вже є open case з тим самим change_hash
 *   - Маркує рекомендацію як 'actioned' з case_id у metrics
 *
 * Викликається cron щогодини або вручну.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { checkSystemHealth } from "../_shared/system-health-guard.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MAX_ACTUATIONS_PER_DAY = 5;
const PROVEN_LIMIT = 3;
const EMERGING_LIMIT = 2;

interface RecRow {
  id: string;
  title: string;
  description: string;
  confidence: number;
  expected_impact: string | null;
  affected_layer: string | null;
  metrics: Record<string, any>;
}

function hashChange(parts: Record<string, any>): string {
  const s = JSON.stringify(parts, Object.keys(parts).sort());
  // Simple djb2 hash → hex; sufficient for dedup bucket
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h) ^ s.charCodeAt(i);
  return Math.abs(h).toString(16).padStart(8, "0") + ":actuator";
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
    // 0. Kill-switch check: read autonomy settings
    const { data: settingsRows } = await sb
      .from("bot_settings")
      .select("key, value")
      .in("key", ["autonomy_enabled", "autonomy_disabled_agents", "autonomy_max_per_day"]);
    const settings: Record<string, any> = {};
    for (const row of settingsRows ?? []) settings[row.key] = row.value;

    const autonomyEnabled = settings.autonomy_enabled !== false;
    const disabledAgents: string[] = Array.isArray(settings.autonomy_disabled_agents)
      ? settings.autonomy_disabled_agents
      : [];
    const maxPerDay: number = typeof settings.autonomy_max_per_day === "number"
      ? settings.autonomy_max_per_day
      : MAX_ACTUATIONS_PER_DAY;

    if (!autonomyEnabled) {
      return new Response(
        JSON.stringify({ ok: true, skipped: true, reason: "autonomy_disabled_by_kill_switch" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 0b. System health guard — block if too many recent rollbacks / open criticals / bad LTV-CAC
    const health = await checkSystemHealth();
    if (!health.ok) {
      try {
        await sb.from("agent_runs").insert({
          function_name: "acos-recommendation-actuator",
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

    // 1. Rate-limit: count actuator-created cases in last 24h
    const since24h = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    const { count: recentCount } = await sb
      .from("tribunal_cases")
      .select("*", { count: "exact", head: true })
      .eq("requested_by", "acos-recommendation-actuator")
      .gte("created_at", since24h);
    const remainingBudget = Math.max(0, maxPerDay - (recentCount ?? 0));

    if (remainingBudget === 0) {
      return new Response(
        JSON.stringify({ ok: true, skipped: true, reason: "daily_budget_exhausted", recent_count: recentCount }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Fetch open recommendations sorted: proven first, then by confidence desc
    const { data: recsRaw, error: recErr } = await sb
      .from("ai_insights")
      .select("id, title, description, confidence, expected_impact, affected_layer, metrics")
      .eq("insight_type", "proactive_recommendation")
      .eq("status", "open")
      .order("created_at", { ascending: false })
      .limit(50);
    if (recErr) throw recErr;

    const recs = (recsRaw ?? []) as RecRow[];
    const proven = recs.filter((r) => r.metrics?.track === "proven").slice(0, PROVEN_LIMIT);
    const emerging = recs.filter((r) => r.metrics?.track === "emerging").slice(0, EMERGING_LIMIT);
    const ordered = [...proven, ...emerging].slice(0, remainingBudget);

    const created: Array<{ rec_id: string; case_id: string; track: string; pattern_key: string }> = [];
    const skipped: Array<{ rec_id: string; reason: string }> = [];

    // Pre-compute all change_hashes and batch-fetch existing duplicate cases.
    const dayBucket = Math.floor(Date.now() / (24 * 3600 * 1000));
    const allHashes = ordered.map((r) => {
      const m = r.metrics || {};
      return hashChange({
        pattern_key: m.pattern_key ?? "unknown",
        agent: m.agent ?? "orchestrator",
        category: m.category ?? r.affected_layer ?? "general",
        day: dayBucket,
      });
    });
    // Guard: empty .in() returns ALL rows, not zero rows.
    const { data: dupRows } = allHashes.length
      ? await sb
          .from("tribunal_cases")
          .select("id, change_hash, status")
          .in("change_hash", allHashes)
          .in("status", ["pending", "prosecuted", "defended", "judged"])
      : { data: [] as any[] };
    const dupMap = new Map((dupRows ?? []).map((d: any) => [d.change_hash, d.id]));

    const insightUpdateOps: Array<{ id: string; payload: Record<string, unknown> }> = [];

    for (const r of ordered) {
      const m = r.metrics || {};
      const patternKey: string = m.pattern_key ?? "unknown";
      const agent: string = m.agent ?? "orchestrator";
      const category: string = m.category ?? r.affected_layer ?? "general";
      const track: string = m.track ?? "proven";

      // Per-agent kill-switch
      if (disabledAgents.includes(agent)) {
        skipped.push({ rec_id: r.id, reason: `agent_disabled:${agent}` });
        continue;
      }

      // Build deterministic change_hash so duplicates are blocked at DB level
      const change_hash = hashChange({ pattern_key: patternKey, agent, category, day: dayBucket });

      // Idempotency: use pre-fetched map instead of per-item DB query
      const dupId = dupMap.get(change_hash);
      if (dupId) {
        skipped.push({ rec_id: r.id, reason: `dup_case:${dupId}` });
        continue;
      }

      const proposed_change = {
        action_type: "replay_pattern",
        pattern_key: patternKey,
        agent,
        category,
        rationale: r.description,
      };

      const context = {
        source_recommendation_id: r.id,
        track,
        success_count: m.success_count,
        failure_count: m.failure_count,
        avg_impact_pct: m.avg_impact_pct,
        recent_uses_14d: m.recent_uses_14d,
        bootstrap_mode: m.bootstrap_mode,
      };

      const { data: caseRow, error: insErr } = await sb
        .from("tribunal_cases")
        .insert({
          source_function: "acos-recommendation-actuator",
          category,
          urgency: track === "proven" ? "normal" : "low",
          proposed_change,
          context,
          change_hash,
          expected_impact: r.expected_impact,
          requested_by: "acos-recommendation-actuator",
        })
        .select("id")
        .single();

      if (insErr || !caseRow) {
        skipped.push({ rec_id: r.id, reason: `insert_failed:${insErr?.message ?? "unknown"}` });
        continue;
      }

      // Collect insight update — applied in batch after the loop.
      insightUpdateOps.push({
        id: r.id,
        payload: {
          status: "actioned",
          metrics: { ...m, actuated_case_id: caseRow.id, actuated_at: new Date().toISOString() },
        },
      });

      created.push({ rec_id: r.id, case_id: caseRow.id, track, pattern_key: patternKey });
    }

    // Batch parallel insight updates instead of sequential per-case writes.
    if (insightUpdateOps.length > 0) {
      await Promise.all(
        insightUpdateOps.map(u => sb.from("ai_insights").update(u.payload).eq("id", u.id))
      );
    }

    try {
      await sb.from("agent_runs").insert({
        function_name: "acos-recommendation-actuator",
        trigger,
        status: "success",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `created=${created.length}, skipped=${skipped.length}, budget_left=${remainingBudget - created.length}`,
        payload: { created, skipped, recent_count: recentCount },
      });
    } catch { /* non-fatal */ }

    return new Response(
      JSON.stringify({
        ok: true,
        budget_used_24h: recentCount,
        budget_remaining: remainingBudget - created.length,
        created,
        skipped,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-recommendation-actuator error", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
