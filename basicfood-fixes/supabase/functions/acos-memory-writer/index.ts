/**
 * ACOS Memory Writer (v2)
 *
 * Закриває цикл навчання: дивиться на ai_actions, які вже були виміряні
 * `acos-roi-tracker` (status='measured', actual_result.delta присутній),
 * та агрегує реальний impact у патерни ai_memory.
 *
 * Критичні зміни проти v1:
 *  - Джерело: status='measured' (не 'applied'), бо ROI Tracker уже вимірив delta.
 *  - Ідемпотентність: позначаємо actual_result.memory_processed_at, щоб не
 *    дублювати інкременти при повторних запусках.
 *  - Verdict базується на actual_result.delta (фактичний % зміни),
 *    а не на власних подіях purchase_completed (які могли вже бути
 *    зважені іншими діями).
 *
 * Логіка verdict:
 *   delta ≥ +5%  → success
 *   delta ≤ −5%  → failure
 *   інакше       → neutral (counters не міняємо, але оновлюємо last_observed_at)
 *
 * Cron: щоденно (01:00 UTC) — запускаємо ПІСЛЯ ROI tracker.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MAX_BATCH = 200;
const SUCCESS_THRESHOLD = 0.05; // +5%
const FAILURE_THRESHOLD = -0.05; // -5%

interface MeasuredAction {
  id: string;
  agent_id: string;
  action_type: string;
  target_entity: string | null;
  target_id: string | null;
  applied_at: string | null;
  measured_at: string | null;
  actual_result: Record<string, unknown> | null;
  status?: string;
  reverted_reason?: string | null;
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

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );
  const startedAt = new Date().toISOString();

  try {
    // Беремо measured ТА reverted actions, які ще не пройшли через memory writer.
    // memory_processed_at у actual_result — наш idempotency-маркер.
    // Reverted дії — це сильний failure-сигнал (хтось/щось ВІДКОТИВ дію),
    // навіть якщо ROI не встиг виміряти delta.
    const { data: actions, error } = await supabase
      .from("ai_actions")
      .select("id, agent_id, action_type, target_entity, target_id, applied_at, measured_at, actual_result, status, reverted_reason")
      .in("status", ["measured", "reverted"])
      .order("measured_at", { ascending: true, nullsFirst: false })
      .limit(MAX_BATCH);

    if (error) throw error;

    const rows = (actions ?? []) as MeasuredAction[];
    const fresh = rows.filter((a) => {
      const mp = (a.actual_result as Record<string, unknown> | null)?.memory_processed_at;
      return !mp;
    });

    if (fresh.length === 0) {
      try {
        await supabase.from("agent_runs").insert({
          function_name: "acos-memory-writer",
          trigger,
          status: "success",
          started_at: startedAt,
          finished_at: new Date().toISOString(),
          summary: `processed=0, candidates=${rows.length}`,
        });
      } catch { /* ignore */ }
      return new Response(
        JSON.stringify({ ok: true, processed: 0, candidates: rows.length, message: "No new measured actions to learn from" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Pre-fetch all existing ai_memory records for this batch to eliminate N reads.
    const allPatternKeys = fresh.map((a) =>
      [a.agent_id, a.action_type, a.target_entity ?? "global"].join(":")
    );
    const { data: existingMemoryRows } = await supabase
      .from("ai_memory")
      .select("id, pattern_key, success_count, failure_count, avg_impact, evidence")
      .in("pattern_key", [...new Set(allPatternKeys)]);
    // Keep a mutable in-memory cache so sequential accumulation across same-key actions is correct.
    type MemoryRow = { id: string; pattern_key: string; success_count: number; failure_count: number; avg_impact: number; evidence: unknown };
    const memoryCache = new Map<string, MemoryRow>(
      (existingMemoryRows ?? []).map((r) => [r.pattern_key, r as MemoryRow])
    );

    let updatedPatterns = 0;
    let success = 0, failure = 0, neutral = 0;
    const patternResults: Array<{ key: string; delta_pct: number; verdict: string }> = [];
    const stampUpdates: Array<{ id: string; actual_result: Record<string, unknown> }> = [];

    for (const action of fresh) {
      const ar = (action.actual_result ?? {}) as Record<string, unknown>;
      const isReverted = action.status === "reverted";
      // Reverted actions: примусово failure з delta=-100% (або з measured delta якщо є).
      // Це гарантує що відкочені патерни ніколи не будуть рекомендовані повторно.
      const deltaRaw = isReverted
        ? Math.min(Number(ar.delta ?? 0), -1.0) // -100% (or measured loss if worse)
        : Number(ar.delta ?? 0);
      const deltaPct = deltaRaw * 100;

      let verdict: "success" | "failure" | "neutral";
      if (isReverted) verdict = "failure";
      else if (deltaRaw >= SUCCESS_THRESHOLD) verdict = "success";
      else if (deltaRaw <= FAILURE_THRESHOLD) verdict = "failure";
      else verdict = "neutral";

      // Pattern key: agent + action_type + target_entity (без target_id для крос-навчання)
      const patternKey = [
        action.agent_id,
        action.action_type,
        action.target_entity ?? "global",
      ].join(":");

      // Категорія для зручності UI: спрощений рядок з action_type
      const category = (action.action_type || "general").toLowerCase();

      // Use pre-fetched cache instead of per-action DB select.
      const existing = memoryCache.get(patternKey) ?? null;

      const incSuccess = verdict === "success" ? 1 : 0;
      const incFailure = verdict === "failure" ? 1 : 0;
      const newSuccess = (existing?.success_count ?? 0) + incSuccess;
      const newFailure = (existing?.failure_count ?? 0) + incFailure;
      const total = newSuccess + newFailure;

      // Rolling average impact (включаючи neutral, бо це теж сигнал про патерн)
      const oldTotalAll = (existing?.success_count ?? 0) + (existing?.failure_count ?? 0)
        + Number((existing?.evidence as Record<string, unknown> | null)?.neutral_count ?? 0);
      const oldAvg = Number(existing?.avg_impact ?? 0);
      const newAvg = oldTotalAll > 0
        ? (oldAvg * oldTotalAll + deltaPct) / (oldTotalAll + 1)
        : deltaPct;

      // Confidence: success rate, але з penalty за малу вибірку
      const sampleBoost = Math.min(1, total / 10);
      const successRate = total > 0 ? newSuccess / total : 0.5;
      const newConfidence = total > 0
        ? Math.max(0.1, Math.min(0.95, successRate * sampleBoost + 0.5 * (1 - sampleBoost)))
        : 0.5;

      const learnedRule = isReverted
        ? `${action.agent_id} + ${action.action_type} → REVERTED on ${action.target_entity ?? "global"} (${action.reverted_reason?.slice(0, 60) ?? "no reason"}, n=${total})`
        : verdict === "success"
        ? `${action.agent_id} + ${action.action_type} → +${deltaPct.toFixed(1)}% on ${action.target_entity ?? "global"} (n=${total})`
        : verdict === "failure"
        ? `${action.agent_id} + ${action.action_type} → ${deltaPct.toFixed(1)}% drop on ${action.target_entity ?? "global"} (n=${total})`
        : `${action.agent_id} + ${action.action_type} → neutral effect (Δ${deltaPct.toFixed(1)}%, n=${total})`;

      const prevNeutral = Number((existing?.evidence as Record<string, unknown> | null)?.neutral_count ?? 0);
      const prevReverted = Number((existing?.evidence as Record<string, unknown> | null)?.reverted_count ?? 0);
      const evidence = {
        last_action_id: action.id,
        last_delta_pct: Math.round(deltaPct * 10) / 10,
        last_scope: ar.scope ?? null,
        last_baseline: ar.baseline ?? null,
        last_current: ar.current ?? null,
        last_evaluated_at: new Date().toISOString(),
        last_was_reverted: isReverted,
        last_reverted_reason: isReverted ? (action.reverted_reason ?? null) : null,
        sample_size: total,
        neutral_count: prevNeutral + (verdict === "neutral" ? 1 : 0),
        reverted_count: prevReverted + (isReverted ? 1 : 0),
      };

      let writeOk = true;
      let writeError: string | null = null;
      if (existing) {
        const { error: updErr } = await supabase.from("ai_memory").update({
          success_count: newSuccess,
          failure_count: newFailure,
          avg_impact: Math.round(newAvg * 10) / 10,
          confidence: Math.round(newConfidence * 100) / 100,
          learned_rule: learnedRule,
          evidence,
          last_observed_at: new Date().toISOString(),
          is_active: true,
        }).eq("id", existing.id);
        if (updErr) { writeOk = false; writeError = updErr.message; }
      } else {
        const { error: insErr } = await supabase.from("ai_memory").insert({
          pattern_key: patternKey,
          agent: action.agent_id,
          category,
          learned_rule: learnedRule,
          success_count: newSuccess,
          failure_count: newFailure,
          avg_impact: Math.round(newAvg * 10) / 10,
          confidence: Math.round(newConfidence * 100) / 100,
          evidence,
          is_active: true,
        });
        if (insErr) { writeOk = false; writeError = insErr.message; }
      }
      if (!writeOk) {
        console.warn(`[memory-writer] write failed for ${patternKey}:`, writeError);
        // Не лічимо як updated_pattern і не ставимо idempotency-stamp,
        // щоб наступний запуск повторно спробував.
        patternResults.push({ key: patternKey, delta_pct: Math.round(deltaPct * 10) / 10, verdict: `error:${writeError?.slice(0, 80) ?? "unknown"}` });
        continue;
      }
      // Update in-memory cache so subsequent actions with the same pattern_key accumulate correctly.
      memoryCache.set(patternKey, { id: existing?.id ?? "", pattern_key: patternKey, success_count: newSuccess, failure_count: newFailure, avg_impact: Math.round(newAvg * 10) / 10, evidence });
      updatedPatterns++;

      // Collect idempotency stamps for batch parallel update at the end.
      const stampedResult = { ...ar, memory_processed_at: new Date().toISOString(), memory_verdict: verdict };
      stampUpdates.push({ id: action.id, actual_result: stampedResult });

      if (verdict === "success") success++;
      else if (verdict === "failure") failure++;
      else neutral++;

      patternResults.push({ key: patternKey, delta_pct: Math.round(deltaPct * 10) / 10, verdict });
    }

    // Parallel idempotency stamps.
    if (stampUpdates.length > 0) {
      await Promise.all(
        stampUpdates.map(({ id, actual_result }) =>
          supabase.from("ai_actions").update({ actual_result }).eq("id", id).catch(() => {})
        ),
      );
    }

    try {
      await supabase.from("agent_runs").insert({
        function_name: "acos-memory-writer",
        trigger,
        status: "success",
        started_at: startedAt,
        finished_at: new Date().toISOString(),
        summary: `processed=${fresh.length}, success=${success}, failure=${failure}, neutral=${neutral}`,
        payload: { sample: patternResults.slice(0, 10) },
      });
    } catch { /* ignore */ }

    return new Response(
      JSON.stringify({
        ok: true,
        processed: fresh.length,
        candidates: rows.length,
        updated_patterns: updatedPatterns,
        success,
        failure,
        neutral,
        results: patternResults,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-memory-writer error", err);
    try {
      await supabase.from("agent_runs").insert({
        function_name: "acos-memory-writer",
        trigger,
        status: "error",
        started_at: startedAt,
        finished_at: new Date().toISOString(),
        error_message: (err as Error).message,
      });
    } catch { /* ignore */ }
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
