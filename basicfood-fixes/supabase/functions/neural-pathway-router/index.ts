// Neural Pathway Router
// Викликається після завершення агента. Дивиться в agent_synapses — чи є
// active/auto_active зв'язки від цього агента, і запускає відповідних
// "нейронів-наступників". Логує все в neural_activation_log.
//
// Виклик: POST { source_agent: string, source_run_id?: string, context?: any }

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MAX_FANOUT = 5;        // не запускати більше N агентів за раз
const MIN_STRENGTH = 0.5;    // не активувати слабкі зв'язки

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  try {
    const body = await req.json().catch(() => ({}));
    const sourceAgent: string | undefined = body.source_agent;
    const sourceRunId: string | undefined = body.source_run_id;
    const ctx = body.context ?? {};
    if (!sourceAgent) return json({ error: "source_agent required" }, 400);

    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // Include trigger stats so we don't need a per-synapse re-fetch later.
    const { data: synapses, error } = await sb
      .from("agent_synapses")
      .select("id, to_agent, strength, status, auto_activate, successful_triggers, failed_triggers, last_activated_at")
      .eq("from_agent", sourceAgent)
      .in("status", ["active", "auto_active"])
      .gte("strength", MIN_STRENGTH)
      .order("strength", { ascending: false })
      .limit(MAX_FANOUT);
    if (error) throw error;

    const targets = (synapses ?? []) as Array<{
      id: string;
      to_agent: string;
      strength: number;
      status: string;
      auto_activate: boolean;
      successful_triggers: number | null;
      failed_triggers: number | null;
      last_activated_at: string | null;
    }>;

    if (targets.length === 0) {
      return json({ ok: true, source: sourceAgent, fired: 0, message: "no active synapses" });
    }

    const supaUrl = Deno.env.get("SUPABASE_URL")!;
    const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
    // Parallelize all HTTP trigger calls instead of sequential fire-and-wait.
    const triggerResults = await Promise.all(
      targets.map(async (syn) => {
        const t0 = Date.now();
        let result: "success" | "failed" | "skipped" | "timeout" = "skipped";
        try {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), 8000);
          const resp = await fetch(`${supaUrl}/functions/v1/${syn.to_agent}`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${anonKey}`,
              "x-neural-trigger": "1",
              "x-source-agent": sourceAgent,
            },
            body: JSON.stringify({ trigger: "neural", source: sourceAgent, context: ctx }),
            signal: ctrl.signal,
          }).catch((e) => {
            if (e.name === "AbortError") result = "timeout";
            throw e;
          });
          clearTimeout(t);
          result = resp.ok ? "success" : "failed";
        } catch {
          if (result !== "timeout") result = "failed";
        }
        return { syn, result, delayMs: Date.now() - t0 };
      }),
    );

    // Batch log inserts + batch synapse stat updates (uses data already fetched).
    const logRows = triggerResults.map(({ syn, result, delayMs }) => ({
      synapse_id: syn.id,
      source_agent: sourceAgent,
      target_agent: syn.to_agent,
      trigger_run_id: sourceRunId ?? null,
      result,
      delay_ms: delayMs,
      context: ctx,
    }));
    const activatedAt = new Date().toISOString();
    await Promise.all([
      sb.from("neural_activation_log").insert(logRows),
      ...triggerResults.map(({ syn, result }) => {
        const isOk = result === "success";
        return sb.from("agent_synapses").update({
          successful_triggers: (syn.successful_triggers ?? 0) + (isOk ? 1 : 0),
          failed_triggers: (syn.failed_triggers ?? 0) + (isOk ? 0 : 1),
          last_activated_at: activatedAt,
        }).eq("id", syn.id);
      }),
    ]);

    const fired = triggerResults.map(({ syn, result, delayMs }) => ({
      to: syn.to_agent, result, delay_ms: delayMs,
    }));
    return json({ ok: true, source: sourceAgent, fired: fired.length, details: fired });
  } catch (e) {
    console.error("pathway-router error", e);
    return json({ error: String((e as Error)?.message ?? e) }, 500);
  }
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
