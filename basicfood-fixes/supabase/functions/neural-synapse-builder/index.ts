// Neural Synapse Builder
// Аналізує agent_runs і ai_actions: знаходить пари агентів, які часто
// активуються один за одним протягом короткого вікна (≤5 хв) — це
// потенційні synapses. Створює/оновлює записи в agent_synapses, рахує
// силу зв'язку (Hebb-style: "neurons that fire together, wire together").
//
// Cron: every 1h.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const WINDOW_MS = 5 * 60 * 1000; // 5 хв
const MIN_CO_ACTIVATIONS = 3;     // мінімум для пропозиції synapse
const AUTO_ACTIVATE_THRESHOLD = 0.7; // strength для auto_active
const AUTO_ACTIVATE_MIN_SAMPLES = 10;

interface RunRow {
  id: string;
  function_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // Беремо runs за останні 7 днів
    const since = new Date(Date.now() - 7 * 86400_000).toISOString();
    const { data: runs, error } = await sb
      .from("agent_runs")
      .select("id, function_name, status, started_at, finished_at, duration_ms")
      .gte("started_at", since)
      .order("started_at", { ascending: true })
      .limit(10000);
    if (error) throw error;

    const rows = (runs ?? []) as RunRow[];
    if (rows.length < 2) {
      return json({ ok: true, message: "not enough data", runs: rows.length });
    }

    // Знаходимо co-activation pairs: для кожного run X шукаємо runs Y,
    // які стартували в межах [X.start, X.start + WINDOW_MS] та X.function != Y.function
    const pairs = new Map<string, {
      from: string;
      to: string;
      count: number;
      success: number;
      fail: number;
      delays: number[];
    }>();

    for (let i = 0; i < rows.length; i++) {
      const a = rows[i];
      const aTime = new Date(a.started_at).getTime();
      for (let j = i + 1; j < rows.length; j++) {
        const b = rows[j];
        const bTime = new Date(b.started_at).getTime();
        const delay = bTime - aTime;
        if (delay > WINDOW_MS) break;
        if (delay <= 0) continue;
        if (a.function_name === b.function_name) continue;

        const key = `${a.function_name}::${b.function_name}`;
        let p = pairs.get(key);
        if (!p) {
          p = { from: a.function_name, to: b.function_name, count: 0, success: 0, fail: 0, delays: [] };
          pairs.set(key, p);
        }
        p.count++;
        if (b.status === "success") p.success++;
        else if (b.status === "failed" || b.status === "error") p.fail++;
        p.delays.push(delay);
      }
    }

    // Upsert в agent_synapses
    let created = 0;
    let updated = 0;
    let activated = 0;

    // Pre-fetch all existing synapses to avoid per-pair N+1 reads.
    const { data: allSynapses, error: synapseFetchError } = await sb
      .from("agent_synapses")
      .select("id, from_agent, to_agent, status, auto_activate");
    if (synapseFetchError) throw synapseFetchError;
    const synapseMap = new Map(
      (allSynapses ?? []).map((s: any) => [
        `${s.from_agent}::${s.to_agent}`,
        s as { id: string; from_agent: string; to_agent: string; status: string; auto_activate: boolean },
      ]),
    );

    const synapseUpdateOps: Array<{ id: string; payload: object }> = [];
    const synapseInsertRows: object[] = [];

    for (const p of pairs.values()) {
      if (p.count < MIN_CO_ACTIVATIONS) continue;

      const successRate = p.count > 0 ? p.success / p.count : 0;
      const freqScore = Math.min(1, p.count / 30);
      const strength = Number((0.4 * freqScore + 0.6 * successRate).toFixed(3));
      const confidence = Number(Math.min(0.95, p.count / 20).toFixed(2));
      const avgDelay = Math.round(p.delays.reduce((s, d) => s + d, 0) / p.delays.length);

      const shouldAutoActivate =
        strength >= AUTO_ACTIVATE_THRESHOLD && p.count >= AUTO_ACTIVATE_MIN_SAMPLES && successRate >= 0.7;

      const existing = synapseMap.get(`${p.from}::${p.to}`);
      const reason = `Спостережено ${p.count} co-activations за 7 днів, success ${Math.round(successRate * 100)}%, середня затримка ${Math.round(avgDelay / 1000)}с.`;

      if (existing) {
        if (existing.status === "rejected") continue;
        const newStatus =
          existing.status === "active" || existing.status === "auto_active"
            ? existing.status
            : shouldAutoActivate
              ? "auto_active"
              : existing.status;
        synapseUpdateOps.push({
          id: existing.id,
          payload: {
            strength, confidence,
            co_activations: p.count,
            successful_triggers: p.success,
            failed_triggers: p.fail,
            avg_delay_ms: avgDelay,
            discovery_reason: reason,
            evidence: { window_days: 7, success_rate: successRate, sample_size: p.count },
            status: newStatus,
            auto_activate: shouldAutoActivate || existing.auto_activate,
          },
        });
        updated++;
        if (newStatus === "auto_active" && existing.status !== "auto_active") activated++;
      } else {
        synapseInsertRows.push({
          from_agent: p.from,
          to_agent: p.to,
          strength, confidence,
          co_activations: p.count,
          successful_triggers: p.success,
          failed_triggers: p.fail,
          avg_delay_ms: avgDelay,
          discovery_reason: reason,
          evidence: { window_days: 7, success_rate: successRate, sample_size: p.count },
          status: shouldAutoActivate ? "auto_active" : "proposed",
          auto_activate: shouldAutoActivate,
        });
        created++;
        if (shouldAutoActivate) activated++;
      }
    }

    // Batch apply all synapse writes after the loop.
    if (synapseUpdateOps.length > 0) {
      const updateResults = await Promise.all(
        synapseUpdateOps.map((u) => sb.from("agent_synapses").update(u.payload).eq("id", u.id)),
      );
      for (const r of updateResults) {
        if (r.error) console.warn("[synapse-builder] synapse update failed:", r.error.message);
      }
    }
    if (synapseInsertRows.length > 0) {
      const { error: insertErr } = await sb.from("agent_synapses").insert(synapseInsertRows);
      if (insertErr) console.warn("[synapse-builder] synapse insert failed:", insertErr.message);
    }

    // Discover pathways: 3-step chains, де кожна пара — active synapse
    const { data: activeSyns } = await sb
      .from("agent_synapses")
      .select("from_agent, to_agent, strength")
      .in("status", ["active", "auto_active"])
      .gte("strength", 0.5);

    const synMap = new Map<string, { to: string; strength: number }[]>();
    for (const s of (activeSyns ?? []) as { from_agent: string; to_agent: string; strength: number }[]) {
      if (!synMap.has(s.from_agent)) synMap.set(s.from_agent, []);
      synMap.get(s.from_agent)!.push({ to: s.to_agent, strength: s.strength });
    }

    // Collect candidate pathways without DB calls; batch pre-fetch and insert.
    const candidatePaths: Array<{
      name: string;
      description: string;
      agent_sequence: string[];
      status: string;
      confidence: number;
      discovery_reason: string;
      evidence: object;
    }> = [];
    outer: for (const [a, bs] of synMap.entries()) {
      for (const b of bs) {
        const cs = synMap.get(b.to);
        if (!cs) continue;
        for (const c of cs) {
          if (c.to === a) continue;
          const pathConfidence = Number((b.strength * c.strength).toFixed(2));
          if (pathConfidence < 0.3) continue;
          candidatePaths.push({
            name: `${a} → ${b.to} → ${c.to}`,
            description: `Виявлено стійкий ланцюжок: ${a} часто запускає ${b.to}, який запускає ${c.to}.`,
            agent_sequence: [a, b.to, c.to],
            status: "proposed",
            confidence: pathConfidence,
            discovery_reason: `Composed from synapses ${a}→${b.to} (${b.strength.toFixed(2)}) і ${b.to}→${c.to} (${c.strength.toFixed(2)}).`,
            evidence: { composed: true, link_strengths: [b.strength, c.strength] },
          });
          if (candidatePaths.length >= 20) break outer;
        }
      }
    }

    let pathwaysCreated = 0;
    if (candidatePaths.length > 0) {
      const pathNames = candidatePaths.map((p) => p.name);
      const { data: existingPaths, error: pathFetchErr } = await sb
        .from("agent_neural_pathways")
        .select("name")
        .in("name", pathNames);
      if (pathFetchErr) console.warn("[synapse-builder] pathway fetch failed:", pathFetchErr.message);
      const existingNames = new Set((existingPaths ?? []).map((p: any) => p.name as string));
      const newPaths = candidatePaths.filter((p) => !existingNames.has(p.name));
      if (newPaths.length > 0) {
        const { error: pathInsertErr } = await sb.from("agent_neural_pathways").insert(newPaths);
        if (pathInsertErr) console.warn("[synapse-builder] pathway insert failed:", pathInsertErr.message);
        else pathwaysCreated = newPaths.length;
      }
    }

    return json({
      ok: true,
      runs_analyzed: rows.length,
      pairs_found: pairs.size,
      synapses_created: created,
      synapses_updated: updated,
      auto_activated: activated,
      pathways_discovered: pathwaysCreated,
    });
  } catch (e) {
    console.error("synapse-builder error", e);
    return json({ error: String((e as Error)?.message ?? e) }, 500);
  }
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
