/**
 * Tribunal Loop Watchdog
 *
 * Monitors the closed-loop learning system end-to-end:
 *
 *   tribunal_cases (24h) → ai_actions:applied (7d) → ai_actions:measured (14d)
 *      → ai_memory updates (14d) → ai_insights:proactive_recommendation (3d)
 *
 * For each stage it computes throughput and the most recent activity timestamp.
 * If any stage is stalled (no activity within its expected SLA window), it emits
 * an `ai_insight` of type `loop_health_alert` with severity & remediation hints,
 * and optionally pings admin Telegram.
 *
 * Idempotent via dedup_bucket (per-day per-stage), so re-runs don't spam.
 *
 * Cron: every 3 hours.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

interface StageDef {
  key: string;
  label: string;
  table: string;
  // SLA: max hours we tolerate without any new row matching `filter`
  sla_hours: number;
  // Function name to suggest as remediation
  remediation_fn: string;
  // Filter applied to most-recent query
  filter?: (q: any) => any;
  // Window for throughput (hours)
  throughput_hours: number;
}

const STAGES: StageDef[] = [
  {
    key: "tribunal_cases",
    label: "Tribunal cases created",
    table: "tribunal_cases",
    sla_hours: 36,
    throughput_hours: 24 * 7,
    remediation_fn: "tribunal-orchestrator",
  },
  {
    key: "actions_applied",
    label: "Actions applied (Enforcer output)",
    table: "ai_actions",
    sla_hours: 72,
    throughput_hours: 24 * 7,
    remediation_fn: "tribunal-enforcer",
    filter: (q) => q.eq("status", "applied"),
  },
  {
    key: "actions_measured",
    label: "Actions measured (ROI tracker)",
    table: "ai_actions",
    sla_hours: 24 * 10, // ROI window is 7d, give headroom
    throughput_hours: 24 * 14,
    remediation_fn: "acos-roi-tracker",
    filter: (q) => q.not("measured_at", "is", null),
  },
  {
    key: "memory_updates",
    label: "Memory pattern updates",
    table: "ai_memory",
    sla_hours: 24 * 10,
    throughput_hours: 24 * 14,
    remediation_fn: "acos-memory-writer",
  },
  {
    key: "proactive_recs",
    label: "Proactive recommendations",
    table: "ai_insights",
    sla_hours: 24 * 7,
    throughput_hours: 24 * 14,
    remediation_fn: "acos-proactive-recommender",
    filter: (q) => q.eq("insight_type", "proactive_recommendation"),
  },
];

async function tg(token: string | undefined, chat: string | undefined, text: string): Promise<boolean> {
  if (!token || !chat) return false;
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true }),
    });
    return r.ok;
  } catch (e) {
    console.warn("[loop-watchdog] tg failed:", String((e as Error)?.message ?? e));
    return false;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));
    const now = Date.now();
    const dedupBucket = Math.floor(now / DAY);
    const stageReports: Array<Record<string, unknown>> = [];
    const alerts: string[] = [];

    // Parallelize all 5×2 stage queries at once instead of sequential per-stage awaits.
    const stageQueryResults = await Promise.all(
      STAGES.map(async (stage) => {
        const throughputSince = new Date(now - stage.throughput_hours * HOUR).toISOString();
        const tsCol = stage.table === "ai_memory" ? "updated_at" : "created_at";
        let countQ = sb.from(stage.table).select("*", { count: "exact", head: true }).gte(tsCol, throughputSince);
        if (stage.filter) countQ = stage.filter(countQ);
        let latestQ = sb.from(stage.table).select(tsCol).order(tsCol, { ascending: false }).limit(1);
        if (stage.filter) latestQ = stage.filter(latestQ);
        const [{ count }, { data: latestRows }] = await Promise.all([countQ, latestQ]);
        return { stage, count, latestRows };
      }),
    );

    const stalledEntries: Array<{ stage: StageDef; count: number | null; latestTs: string | null; ageHours: number }> = [];

    for (const { stage, count, latestRows } of stageQueryResults) {
      const latestRaw = (latestRows?.[0] as Record<string, string> | undefined);
      const latestTs = latestRaw ? Object.values(latestRaw)[0] : null;
      const ageHours = latestTs ? (now - new Date(latestTs).getTime()) / HOUR : Infinity;
      const stalled = ageHours > stage.sla_hours;

      stageReports.push({
        key: stage.key,
        label: stage.label,
        throughput: count ?? 0,
        throughput_window_hours: stage.throughput_hours,
        last_activity_at: latestTs,
        age_hours: latestTs ? Math.round(ageHours * 10) / 10 : null,
        sla_hours: stage.sla_hours,
        stalled,
      });

      if (stalled) {
        const ageLabel = latestTs
          ? `${Math.round(ageHours)}h (SLA ${stage.sla_hours}h)`
          : `no activity ever`;
        alerts.push(`• <b>${stage.label}</b>: ${ageLabel} — try /${stage.remediation_fn}`);
        stalledEntries.push({ stage, count, latestTs, ageHours });
      }
    }

    // Batch pre-fetch existing insights for all stalled stages; batch insert missing ones.
    if (stalledEntries.length > 0) {
      const stalledTitles = stalledEntries.map((e) => `Loop stage stalled: ${e.stage.key}`);
      const { data: existingInsights } = await sb
        .from("ai_insights")
        .select("title")
        .eq("insight_type", "loop_health_alert")
        .eq("dedup_bucket", dedupBucket)
        .in("title", stalledTitles);
      const existingTitles = new Set((existingInsights ?? []).map((i: any) => i.title as string));

      const newInsightRows = stalledEntries
        .filter((e) => !existingTitles.has(`Loop stage stalled: ${e.stage.key}`))
        .map(({ stage, count, latestTs, ageHours }) => ({
          insight_type: "loop_health_alert",
          title: `Loop stage stalled: ${stage.key}`,
          description:
            `Stage "${stage.label}" hasn't had new activity in ${latestTs ? Math.round(ageHours) + "h" : "the tracked window"} ` +
            `(SLA ${stage.sla_hours}h). Throughput in last ${stage.throughput_hours}h = ${count ?? 0}. ` +
            `Suggested remediation: invoke "${stage.remediation_fn}" or check its cron schedule and recent logs.`,
          confidence: 0.9,
          risk_level: "medium",
          affected_layer: "learning_loop",
          expected_impact: "Restoring this stage unblocks downstream agents and ROI measurement",
          metrics: {
            stage: stage.key,
            age_hours: latestTs ? Math.round(ageHours * 10) / 10 : null,
            sla_hours: stage.sla_hours,
            throughput: count ?? 0,
            throughput_window_hours: stage.throughput_hours,
            remediation_fn: stage.remediation_fn,
          },
          dedup_bucket: dedupBucket,
          status: "open",
        }));

      if (newInsightRows.length > 0) {
        await sb.from("ai_insights").insert(newInsightRows);
      }
    }

    // Send single consolidated Telegram alert if any stage is stalled
    if (alerts.length > 0) {
      const token = Deno.env.get("TELEGRAM_API_KEY_1");
      const adminChat = Deno.env.get("TELEGRAM_ADMIN_CHAT_ID");
      if (token && adminChat) {
        const text =
          `🛡️ <b>Tribunal Loop Watchdog</b>\n\n` +
          `Виявлено ${alerts.length} стадій із пропущеним SLA:\n\n` +
          alerts.join("\n");
        await tg(token, adminChat, text);
      }
    }

    try {
      await sb.from("agent_runs").insert({
        function_name: "tribunal-loop-watchdog",
        trigger: detectTrigger(req, body),
        status: alerts.length === 0 ? "success" : "partial",
        started_at: new Date(now - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `stages=${STAGES.length}, stalled=${alerts.length}`,
        payload: { stageReports },
      });
    } catch { /* non-fatal */ }

    return new Response(
      JSON.stringify({
        ok: true,
        stages: stageReports,
        stalled_count: alerts.length,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("tribunal-loop-watchdog error", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
