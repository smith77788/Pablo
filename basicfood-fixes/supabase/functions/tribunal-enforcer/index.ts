/**
 * 🟦 Enforcer — застосовує / блокує дію згідно вердикту.
 *
 * Читає `tribunal_verdicts` зі статусом enforcement_status='pending', виконує:
 *  - approve / approve_with_conditions → викликає відповідну "executor"-функцію
 *    з payload з proposed_change + conditions з вердикту.
 *  - reject → пише в ai_memory failure_count++, статус кейса='rejected'.
 *  - defer_to_human → створює insight для адміна.
 *
 * Виконавчі функції (`source_function` → real function, передається payload):
 *  - acos-auto-pricing-engine → call self з flag from_tribunal=true
 *  - acos-auto-promote-engine → те саме
 *  - acos-elasticity-auto-apply → те саме
 *  - acos-seo-rewriter → те саме
 *  - admin-telegram (broadcast) → telegram-broadcast виклик
 *  - acos-stale-promo-cleaner → flag from_tribunal=true
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger, finishAgentRun, startAgentRun } from "../_shared/agent-logger.ts";
import { recordEnforcementOutcome } from "../_shared/enforcer-memory.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
/**
 * Bridge tribunal verdicts → public.ai_actions.
 *
 * Without this, the downstream learning loop (ROI tracker, memory writer,
 * proactive recommender, weekly digest, AI Actions admin page) has nothing
 * to consume because Tribunal mutates only its own `tribunal_*` tables.
 *
 * Idempotent on (case_id, verdict_id): if a row already exists for this
 * verdict, we update its status instead of inserting a duplicate.
 */
async function bridgeToAiActions(
  supabase: any,
  args: {
    caseRow: any;
    verdictId: string;
    actionStatus: "applied" | "rejected" | "deferred" | "failed";
    invokeRes?: any;
    reasoning?: string | null;
  },
): Promise<void> {
  try {
    const { caseRow, verdictId, actionStatus, invokeRes, reasoning } = args;
    const proposed = (caseRow.proposed_change ?? {}) as Record<string, any>;

    // Best-effort target extraction
    const targetEntity: string | null =
      proposed.target_entity ?? proposed.entity ?? proposed.kind ??
      (proposed.product_id ? "product" : proposed.page_path ? "page" : null);
    const rawTargetId = proposed.target_id ?? proposed.product_id ?? proposed.entity_id ?? null;
    // ai_actions.target_id is uuid → only pass UUIDs.
    const uuidRe = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const targetId: string | null = typeof rawTargetId === "string" && uuidRe.test(rawTargetId) ? rawTargetId : null;

    const agentId = `tribunal:${caseRow.source_function}`.slice(0, 200);
    const actionType = (caseRow.category ?? "unknown").toString().slice(0, 100);

    const nowIso = new Date().toISOString();
    const baseRow: Record<string, any> = {
      agent_id: agentId,
      action_type: actionType,
      target_entity: targetEntity,
      target_id: targetId,
      status: actionStatus,
      expected_impact: caseRow.expected_impact ?? null,
      parameters: {
        case_id: caseRow.id,
        verdict_id: verdictId,
        source_function: caseRow.source_function,
        proposed_change: proposed,
      },
      actual_result: invokeRes
        ? { invoke_status: invokeRes.status ?? null, ok: !!invokeRes.ok, body_preview: JSON.stringify(invokeRes.body ?? {}).slice(0, 500) }
        : reasoning
          ? { reasoning: String(reasoning).slice(0, 500) }
          : {},
      applied_at: actionStatus === "applied" ? nowIso : null,
    };

    // Idempotency: look up by case_id in parameters json (cheap because we
    // expect <= 1 verdict per case in practice).
    const { data: existing } = await supabase
      .from("ai_actions")
      .select("id")
      .contains("parameters", { case_id: caseRow.id, verdict_id: verdictId })
      .maybeSingle();

    if (existing) {
      await supabase.from("ai_actions").update({
        status: actionStatus,
        applied_at: baseRow.applied_at ?? null,
        actual_result: baseRow.actual_result,
        updated_at: nowIso,
      }).eq("id", existing.id);
    } else {
      const { error } = await supabase.from("ai_actions").insert(baseRow);
      if (error) console.warn("[enforcer→ai_actions] insert failed:", error.message);
    }
  } catch (e) {
    console.warn("[enforcer→ai_actions] bridge crashed:", String((e as Error)?.message ?? e));
  }
}

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

interface VerdictRow {
  id: string;
  case_id: string;
  verdict: string;
  conditions: Record<string, unknown>;
  reasoning: string | null;
  enforcement_status: string;
}

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

async function invokeEdge(supabase: any, name: string, payload: unknown) {
  const url = `${SUPABASE_URL}/functions/v1/${name}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
      apikey: SUPABASE_SERVICE_ROLE_KEY,
    },
    body: JSON.stringify(payload),
  });
  const text = await res.text();
  let json: any = null;
  try { json = JSON.parse(text); } catch { json = { raw: text }; }
  return { ok: res.ok, status: res.status, body: json };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const body = await req.json().catch(() => ({}));
  const targetCaseId = body.case_id ?? null;
  const trigger = req.headers.get("x-tribunal-trigger") === "orchestrator" || body.from_orchestrator === true
    ? "orchestrator"
    : detectTrigger(req, body);
  const runId = await startAgentRun("tribunal-enforcer", trigger, {
    case_id: targetCaseId,
    body_keys: Object.keys(body ?? {}),
  });
  try {
    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

    let q = supabase
      .from("tribunal_verdicts")
      .select("id, case_id, verdict, conditions, reasoning, enforcement_status")
      .eq("enforcement_status", "pending")
      .limit(20);

    if (targetCaseId) q = q.eq("case_id", targetCaseId);

    const { data: pending, error } = await q;
    if (error) throw new Error(error.message);

    // Batch-prefetch all tribunal_cases in one query to eliminate per-verdict reads
    const pendingRows = (pending as VerdictRow[]) ?? [];
    const caseIds = [...new Set(pendingRows.map((v) => v.case_id))];
    const { data: caseRows } = caseIds.length
      ? await supabase.from("tribunal_cases").select("*").in("id", caseIds)
      : { data: [] as any[] };
    const caseMap = new Map<string, any>((caseRows ?? []).map((c: any) => [c.id, c]));

    // Process verdicts in parallel — each verdict is scoped to its own IDs
    const results: any[] = (await Promise.all(pendingRows.map(async (v): Promise<any> => {
      const kase = caseMap.get(v.case_id) ?? null;
      if (!kase) {
        await supabase.from("tribunal_verdicts").update({
          enforcement_status: "failed",
          enforcement_result: { error: "case_not_found" },
        }).eq("id", v.id);
        return null;
      }

      // REJECT — нічого не виконуємо, оновлюємо memory
      if (v.verdict === "reject") {
        await Promise.all([
          supabase.from("tribunal_verdicts").update({
            enforcement_status: "skipped",
            enforced_at: new Date().toISOString(),
            enforcement_result: { skipped_reason: "rejected_by_judge" },
          }).eq("id", v.id),
          supabase.from("tribunal_cases").update({ status: "rejected" }).eq("id", v.case_id),
          supabase.from("ai_memory").upsert({
            agent: "tribunal",
            category: kase.category,
            pattern_key: `${kase.source_function}:${kase.change_hash.slice(0, 16)}`,
            learned_rule: `Tribunal rejected: ${v.reasoning?.slice(0, 200) ?? ""}`,
            failure_count: 1,
            confidence: 0.6,
            evidence: { case_id: v.case_id, verdict_id: v.id },
            last_observed_at: new Date().toISOString(),
          }, { onConflict: "agent,pattern_key" }),
          bridgeToAiActions(supabase, {
            caseRow: kase, verdictId: v.id, actionStatus: "rejected", reasoning: v.reasoning,
          }),
        ]);
        return { case_id: v.case_id, action: "rejected" };
      }

      // DEFER → insight для адміна
      if (v.verdict === "defer_to_human") {
        await Promise.all([
          supabase.from("ai_insights").insert({
            insight_type: "tribunal_defer",
            title: `Трибунал передає рішення людині (${kase.category})`,
            description: v.reasoning ?? "Потрібне ручне рішення",
            risk_level: "high",
            affected_layer: kase.source_function,
            status: "open",
            confidence: 0.7,
            metrics: { case_id: v.case_id, proposed_change: kase.proposed_change },
          }),
          supabase.from("tribunal_verdicts").update({
            enforcement_status: "skipped",
            enforced_at: new Date().toISOString(),
            enforcement_result: { skipped_reason: "deferred_to_human" },
          }).eq("id", v.id),
          supabase.from("tribunal_cases").update({ status: "deferred" }).eq("id", v.case_id),
          bridgeToAiActions(supabase, {
            caseRow: kase, verdictId: v.id, actionStatus: "deferred", reasoning: v.reasoning,
          }),
        ]);
        return { case_id: v.case_id, action: "deferred" };
      }

      // APPROVE / APPROVE_WITH_CONDITIONS → виконуємо
      const payload = {
        from_tribunal: true,
        case_id: v.case_id,
        verdict_id: v.id,
        proposed_change: kase.proposed_change,
        conditions: v.conditions,
      };

      let invokeRes: any;
      try {
        invokeRes = await invokeEdge(supabase, kase.source_function, payload);
      } catch (e: any) {
        invokeRes = { ok: false, error: String(e?.message ?? e) };
      }

      const success = !!invokeRes?.ok;
      await Promise.all([
        supabase.from("tribunal_verdicts").update({
          enforcement_status: success ? "enforced" : "failed",
          enforced_at: new Date().toISOString(),
          enforcement_result: invokeRes,
        }).eq("id", v.id),
        supabase.from("tribunal_cases").update({
          status: success ? "enforced" : "error",
        }).eq("id", v.case_id),
        recordEnforcementOutcome({
          source_function: kase.source_function,
          category: kase.category,
          success,
          case_id: v.case_id,
          result_summary: success
            ? `enforced via ${kase.source_function}`
            : `failed: status=${invokeRes?.status ?? "?"}`,
        }),
        bridgeToAiActions(supabase, {
          caseRow: kase, verdictId: v.id,
          actionStatus: success ? "applied" : "failed",
          invokeRes,
        }),
      ]);

      return { case_id: v.case_id, action: success ? "enforced" : "failed", target: kase.source_function };
    }))).filter(Boolean);

    await finishAgentRun(runId, {
      status: results.some((r) => r.action === "failed") ? "partial" : "success",
      summary: `processed=${results.length}, failed=${results.filter((r) => r.action === "failed").length}`,
      payload: { results: results.slice(0, 10), target_case_id: targetCaseId },
    });

    return new Response(JSON.stringify({ ok: true, processed: results.length, results }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    await finishAgentRun(runId, {
      status: "error",
      errorMessage: String(e?.message ?? e).slice(0, 2000),
    });
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
