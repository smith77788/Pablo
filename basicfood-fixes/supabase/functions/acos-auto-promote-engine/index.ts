// ACOS Auto-Promote Engine — escalates ai_insights → ai_actions when memory
// patterns prove the action type is safe. Each candidate is now routed
// through the Tribunal: the prosecutor/judge get to veto a promotion before
// `ai_actions` is touched. The enforcer calls back into this function with
// `from_tribunal=true` to perform the real insert.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { enqueueTribunalCase } from "../_shared/tribunal.ts";
import { checkSystemHealth } from "../_shared/system-health-guard.ts";
import { detectTrigger, beginQuickAgentRun } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const ALLOWED_ACTION_TYPES = new Set([
  "seo_copy_update",
  "product_badge_toggle",
  "bundle_suggestion",
]);

const FORBIDDEN_PATTERNS = [
  "price", "checkout", "auth", "payment", "order_", "user_", "delete", "drop", "rls",
];

const MIN_CONFIDENCE = 0.85;
const MIN_SUCCESS = 5;
const MAX_BATCH = 5;

interface InsightRow {
  id: string;
  insight_type: string;
  title: string;
  description: string;
  affected_layer: string | null;
  confidence: number;
  metrics: Record<string, unknown>;
}

interface MemoryRow {
  pattern_key: string;
  agent: string;
  category: string;
  confidence: number;
  success_count: number;
  failure_count: number;
  avg_impact: number;
  learned_rule: string;
}

const layerToAgent = (layer: string | null, insightType: string): string => {
  const l = (layer || "").toLowerCase();
  const t = (insightType || "").toLowerCase();
  if (l === "seo" || t.includes("seo") || t.includes("content")) return "seo";
  if (l === "stability" || l === "checkout" || t.includes("rollback") || t.includes("friction")) return "stability";
  return "growth";
};

const insightToActionType = (insightType: string): string | null => {
  const t = insightType.toLowerCase();
  if (t.includes("seo") || t.includes("content")) return "seo_copy_update";
  if (t.includes("bestseller") || t.includes("badge") || t.includes("sale_signal")) return "product_badge_toggle";
  if (t.includes("bundle") || t.includes("affinity")) return "bundle_suggestion";
  return null;
};

const isForbidden = (text: string): boolean => {
  const lower = text.toLowerCase();
  return FORBIDDEN_PATTERNS.some(p => lower.includes(p));
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-auto-promote-engine", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const reqBody = await req.json().catch(() => ({}));

    if (reqBody?.from_tribunal === true) {
      return await applyApprovedAction(supabase, reqBody);
    }

    // System Health Guard — block new tribunal enqueues if autonomy stress detected.
    const health = await checkSystemHealth();
    if (!health.ok) {
      try {
        await supabase.from("agent_runs").insert({
          function_name: "acos-auto-promote-engine",
          trigger: detectTrigger(req, reqBody),
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

    const since = new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString();
    const [insightsRes, memoryRes] = await Promise.all([
      supabase
        .from("ai_insights")
        .select("id, insight_type, title, description, affected_layer, confidence, metrics")
        .eq("status", "new")
        .gte("created_at", since)
        .order("created_at", { ascending: false })
        .limit(50),
      supabase
        .from("ai_memory")
        .select("pattern_key, agent, category, confidence, success_count, failure_count, avg_impact, learned_rule")
        .eq("is_active", true)
        .gte("confidence", MIN_CONFIDENCE)
        .gte("success_count", MIN_SUCCESS),
    ]);

    if (insightsRes.error) throw insightsRes.error;
    if (memoryRes.error) throw memoryRes.error;

    const insights = (insightsRes.data ?? []) as InsightRow[];
    const memory = (memoryRes.data ?? []) as MemoryRow[];

    if (insights.length === 0 || memory.length === 0) {
      return new Response(
        JSON.stringify({
          ok: true,
          processed: 0,
          message: insights.length === 0
            ? "No new insights"
            : "No proven memory patterns yet (need ≥5 successes + conf≥0.85)",
          memory_count: memory.length,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const queued: Array<{ insight_id: string; action_type: string; case_id: string; reused: boolean }> = [];
    const skipped: Array<{ insight_id: string; reason: string }> = [];

    // Synchronous classification pass — no DB/HTTP calls
    interface ReadyItem { insight: typeof insights[0]; actionType: string; agent: string; memMatch: typeof memory[0] }
    const readyItems: ReadyItem[] = [];
    for (const insight of insights) {
      if (readyItems.length >= MAX_BATCH) {
        skipped.push({ insight_id: insight.id, reason: "batch_limit_reached" });
        continue;
      }
      const actionType = insightToActionType(insight.insight_type);
      if (!actionType || !ALLOWED_ACTION_TYPES.has(actionType)) {
        skipped.push({ insight_id: insight.id, reason: `not_whitelisted: ${insight.insight_type}` });
        continue;
      }
      if (isForbidden(insight.title) || isForbidden(insight.description)) {
        skipped.push({ insight_id: insight.id, reason: "forbidden_content_detected" });
        continue;
      }
      const sim = (insight.metrics as Record<string, unknown>)?.simulation as
        | { verdict?: string; projected_delta_uah?: number } | undefined;
      if (sim?.verdict === "negative") {
        skipped.push({ insight_id: insight.id, reason: `simulator_negative:${sim.projected_delta_uah ?? 0}uah` });
        continue;
      }
      const agent = layerToAgent(insight.affected_layer, insight.insight_type);
      const memMatch = memory.find((m) =>
        m.agent === agent && (m.category === actionType || m.category === insight.insight_type)
      );
      if (!memMatch) {
        skipped.push({ insight_id: insight.id, reason: "no_proven_memory_match" });
        continue;
      }
      readyItems.push({ insight, actionType, agent, memMatch });
    }

    // Parallel tribunal enqueues
    const enqResults = await Promise.all(readyItems.map(async ({ insight, actionType, agent, memMatch }) => {
      const enq = await enqueueTribunalCase({
        source_function: "acos-auto-promote-engine",
        category: actionType === "seo_copy_update" ? "seo" : "other",
        urgency: "low",
        proposed_change: {
          kind: "ai_action_promotion",
          insight_id: insight.id,
          action: {
            agent_id: agent,
            action_type: actionType,
            target_entity: insight.affected_layer,
            target_id: null,
            status: "executed",
            source_insight_id: insight.id,
            expected_impact: `Avg +${memMatch.avg_impact.toFixed(1)}% (memory pattern, ${memMatch.success_count}✓/${memMatch.failure_count}✗)`,
            parameters: {
              insight_type: insight.insight_type,
              pattern_key: memMatch.pattern_key,
              memory_confidence: memMatch.confidence,
              memory_avg_impact: memMatch.avg_impact,
              auto_promoted: true,
              source: "acos-auto-promote",
            },
          },
        },
        context: {
          insight_title: insight.title,
          memory_pattern: memMatch.pattern_key,
          memory_success: memMatch.success_count,
          memory_failure: memMatch.failure_count,
        },
        expected_impact: `+${memMatch.avg_impact.toFixed(1)}% based on ${memMatch.success_count} historical successes`,
      });
      return { insight_id: insight.id, action_type: actionType, case_id: enq.case_id, reused: enq.reused };
    }));
    queued.push(...enqResults);

    __agent.success();
    return new Response(
      JSON.stringify({
        ok: true,
        processed: insights.length,
        queued_count: queued.length,
        skipped_count: skipped.length,
        queued,
        skipped: skipped.slice(0, 10),
        memory_patterns_available: memory.length,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    __agent.error(err);
    console.error("acos-auto-promote-engine error", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});

async function applyApprovedAction(
  supabase: any,
  body: { proposed_change?: Record<string, unknown>; case_id?: string },
): Promise<Response> {
  const change = body.proposed_change as
    | { insight_id?: string; action?: Record<string, unknown> } | undefined;
  if (!change?.action || !change.insight_id) {
    return new Response(
      JSON.stringify({ ok: false, error: "missing_proposed_change" }),
      { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  const actionPayload = { ...change.action } as Record<string, unknown>;
  if (actionPayload.status === "applied") actionPayload.status = "executed";
  const { error: actionErr } = await supabase.from("ai_actions").insert({
    ...actionPayload,
    applied_at: new Date().toISOString(),
  });

  if (actionErr) {
    return new Response(
      JSON.stringify({ ok: false, error: actionErr.message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  await supabase
    .from("ai_insights")
    .update({ status: "auto_applied" })
    .eq("id", change.insight_id);
    return new Response(
    JSON.stringify({ ok: true, insight_id: change.insight_id, case_id: body.case_id }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
}
