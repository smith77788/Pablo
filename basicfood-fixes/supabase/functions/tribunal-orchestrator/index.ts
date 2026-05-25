/**
 * Tribunal Orchestrator — головний цикл.
 * Для кожного pending case послідовно: prosecutor → advocate → judge → enforcer.
 * Викликається з UI (manual run) або через cron кожні 5 хв.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MAX_BATCH = 8;

const CRON_SECRET = Deno.env.get("CRON_SECRET") ?? "";

async function callEdge(name: string, body: unknown) {
  const payload = typeof body === "object" && body !== null
    ? { ...(body as Record<string, unknown>), from_orchestrator: true }
    : { from_orchestrator: true };
  const res = await fetch(`${SUPABASE_URL}/functions/v1/${name}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-tribunal-trigger": "orchestrator",
      "x-cron-secret": CRON_SECRET,
      Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
      apikey: SUPABASE_SERVICE_ROLE_KEY,
    },
    body: JSON.stringify(payload),
  });
  const text = await res.text();
  let json: any = null;
  try { json = JSON.parse(text); } catch { json = { raw: text }; }
  if (!res.ok) throw new Error(`${name} ${res.status}: ${JSON.stringify(json).slice(0, 200)}`);
  return json;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  try {
    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
    const body = await req.json().catch(() => ({}));
    const onlyCaseId: string | null = body.case_id ?? null;

    // Крок 1: expire будь-який нон-терминальний кейс старший за 24h.
    // Раніше експайрилися лише `pending`, через що `prosecuted`/`defended`
    // після transient-збоїв накопичувалися без обмежень.
    const expireCutoff = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    await supabase.from("tribunal_cases")
      .update({ status: "expired" })
      .lt("created_at", expireCutoff)
      .in("status", ["pending", "prosecuted", "defended"]);

    // Крок 1b: підхопити "застряглі" кейси, де verdict вже винесений,
    // але enforcer не дійшов (timeouts / rate limits). Прогоняємо через enforcer
    // без повторного виклику prosecutor/advocate/judge.
    const { data: stuck } = await supabase.from("tribunal_cases")
      .select("id")
      .in("status", ["judged", "rejected"])
      .lt("created_at", new Date(Date.now() - 5 * 60 * 1000).toISOString())
      .limit(MAX_BATCH);
    await Promise.all((stuck ?? []).map(async (s) => {
      try {
        await callEdge("tribunal-enforcer", { case_id: s.id });
      } catch (e) {
        console.warn(`[orchestrator] enforcer retry failed for ${s.id}:`, String((e as Error)?.message ?? e));
      }
    }));

    let q = supabase.from("tribunal_cases")
      .select("id, status, urgency")
      .in("status", ["pending", "prosecuted", "defended", "judged"])
      .order("urgency", { ascending: false })
      .order("created_at", { ascending: true })
      .limit(MAX_BATCH);
    if (onlyCaseId) q = q.eq("id", onlyCaseId);

    const { data: cases, error } = await q;
    if (error) throw new Error(error.message);

    // Heuristic: detect transient AI provider failures (rate limits, quota, timeouts)
    // worth retrying vs. terminal errors (bad request, RLS, code bug). Transient
    // errors get bounced back to `pending` with a retry counter (max 3) so the
    // pipeline self-recovers when provider quota refreshes — no shouting alert.
    const TRANSIENT_PATTERNS = [
      /\b429\b/i,
      /rate ?limit/i,
      /too many requests/i,
      /quota/i,
      /\b503\b/i,
      /\b504\b/i,
      /timeout/i,
      /timed out/i,
      /All AI providers failed/i,
      /ECONN/i,
      /fetch failed/i,
    ];
    const isTransient = (msg: string) => TRANSIENT_PATTERNS.some((re) => re.test(msg));
    const MAX_RETRIES = 3;

    // Process cases in parallel — each case's pipeline is sequential internally
    // but cases are independent of each other.
    const flow: any[] = await Promise.all((cases ?? []).map(async (k): Promise<any> => {
      const log: any = { case_id: k.id, steps: [] };
      try {
        if (k.status === "pending") {
          await callEdge("tribunal-prosecutor", { case_id: k.id });
          log.steps.push("prosecutor");
        }
        // re-fetch status
        const { data: k2 } = await supabase.from("tribunal_cases").select("status").eq("id", k.id).single();
        if (k2?.status === "prosecuted") {
          await callEdge("tribunal-advocate", { case_id: k.id });
          log.steps.push("advocate");
        }
        const { data: k3 } = await supabase.from("tribunal_cases").select("status").eq("id", k.id).single();
        if (k3?.status === "defended") {
          await callEdge("tribunal-judge", { case_id: k.id });
          log.steps.push("judge");
        }
        const { data: k4 } = await supabase.from("tribunal_cases").select("status").eq("id", k.id).single();
        if (k4?.status === "judged" || k4?.status === "rejected") {
          await callEdge("tribunal-enforcer", { case_id: k.id });
          log.steps.push("enforcer");
        }
        log.ok = true;
      } catch (e: any) {
        log.ok = false;
        log.error = String(e?.message ?? e);
        log.transient = isTransient(log.error);

        // Read existing context to merge — never blow away original payload
        const { data: existing } = await supabase
          .from("tribunal_cases")
          .select("context, status")
          .eq("id", k.id)
          .maybeSingle();
        const prevCtx = (existing?.context as Record<string, unknown> | null) ?? {};
        const prevRetries = Number((prevCtx as any).retry_count ?? 0);

        if (log.transient && prevRetries < MAX_RETRIES) {
          // Bounce back — but only revert status if NOT past prosecutor
          // (i.e. only `pending` failures retry-from-scratch). For later stages,
          // keep the current status so orchestrator picks up where it left off.
          const nextStatus = existing?.status === "pending" ? "pending" : existing?.status;
          await supabase.from("tribunal_cases").update({
            status: nextStatus,
            context: {
              ...prevCtx,
              retry_count: prevRetries + 1,
              last_transient_error: log.error.slice(0, 500),
              last_retry_at: new Date().toISOString(),
            },
          }).eq("id", k.id);
          log.deferred_retry = prevRetries + 1;
        } else {
          await supabase.from("tribunal_cases").update({
            status: "error",
            context: {
              ...prevCtx,
              error: log.error.slice(0, 1000),
              error_at: new Date().toISOString(),
              retry_count: prevRetries,
              terminal: !log.transient,
            },
          }).eq("id", k.id);
        }
      }
      return log;
    }));

    try {
      const failed = flow.filter((f) => !f.ok).length;
      await supabase.from("agent_runs").insert({
        function_name: "tribunal-orchestrator",
        trigger: detectTrigger(req, body),
        status: failed === 0 ? "success" : "partial",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `processed=${flow.length}, failed=${failed}`,
        payload: { flow_sample: flow.slice(0, 5) },
      });
    } catch { /* ignore */ }

    return new Response(JSON.stringify({ ok: true, processed: flow.length, flow }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
