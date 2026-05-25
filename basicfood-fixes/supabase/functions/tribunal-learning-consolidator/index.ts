/**
 * Tribunal Learning Consolidator
 *
 * Cron: every 6h.
 *
 * Що робить:
 *  1. Бере ai_actions за останні 7 днів зі статусом applied/measured.
 *  2. Групує за (agent, action_type) і рахує:
 *      - applied count
 *      - average actual_result.delta (якщо є)
 *      - success_rate (status='measured' з позитивним impact)
 *  3. Записує/оновлює правило в ai_memory:
 *      pattern_key = `learning:{agent}:{action_type}`
 *      learned_rule = "Дія X для Y має успіх Z% (avg impact +N)"
 *  4. Деактивує правила у яких success_rate < 30% та >= 5 застосувань
 *     (тобто Tribunal має уникати таких дій).
 *
 *  Це створює замкнутий цикл навчання:
 *  Tribunal вирішує → Enforcer виконує → ROI Collector міряє → Consolidator вчиться → Tribunal стає розумнішим.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

interface ActionRow {
  agent_id: string;
  action_type: string;
  status: string;
  actual_result: any;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const body = await req.clone().json().catch(() => ({}));

  return await withAgentRun("tribunal-learning-consolidator", detectTrigger(req, body), async () => {
    const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
    const since = new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString();

    const { data: actions, error } = await sb
      .from("ai_actions")
      .select("agent_id, action_type, status, actual_result")
      .in("status", ["applied", "measured", "reverted"])
      .gte("applied_at", since)
      .limit(2000);
    if (error) throw error;

    const groups = new Map<string, ActionRow[]>();
    for (const a of (actions ?? []) as ActionRow[]) {
      const key = `${a.agent_id}::${a.action_type}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(a);
    }

    let updated = 0, deactivated = 0, created = 0;

    // Batch pre-fetch all ai_memory records for the groups we're about to process.
    const allPatternKeys = [...groups.keys()].map((key) => {
      const [agent, action_type] = key.split("::");
      return `learning:${agent}:${action_type}`.slice(0, 200);
    });
    const { data: existingMemRows } = await sb
      .from("ai_memory")
      .select("id, is_active, pattern_key")
      .eq("agent", "tribunal")
      .in("pattern_key", allPatternKeys);
    const memMap = new Map(
      (existingMemRows ?? []).map((r: any) => [r.pattern_key as string, r as { id: string; is_active: boolean; pattern_key: string }]),
    );

    const updateOps: Array<{ id: string; payload: object }> = [];
    const insertRows: object[] = [];

    for (const [key, rows] of groups.entries()) {
      const [agent, action_type] = key.split("::");
      const total = rows.length;
      const reverted = rows.filter((r) => r.status === "reverted").length;
      const measured = rows.filter((r) => r.status === "measured");
      const positive = measured.filter((r) => {
        const d = Number(r.actual_result?.delta ?? r.actual_result?.impact ?? 0);
        return d > 0;
      });
      const successCount = positive.length;
      const failureCount = reverted + (measured.length - positive.length);
      const successRate = total > 0 ? successCount / total : 0;
      const avgImpact = measured.length > 0
        ? measured.reduce((s, r) => s + Number(r.actual_result?.delta ?? r.actual_result?.impact ?? 0), 0) / measured.length
        : 0;

      const pattern_key = `learning:${agent}:${action_type}`.slice(0, 200);
      const ruleText = `Дія "${action_type}" агента "${agent}" має успіх ${Math.round(successRate * 100)}% за ${total} застосувань. Середній impact: ${avgImpact.toFixed(2)}.`;
      const isActive = !(total >= 5 && successRate < 0.3);

      const existing = memMap.get(pattern_key);

      const payload = {
        agent: "tribunal",
        pattern_key,
        category: "action-effectiveness",
        learned_rule: ruleText,
        success_count: successCount,
        failure_count: failureCount,
        avg_impact: Number(avgImpact.toFixed(2)),
        confidence: Math.min(0.95, total / 30),
        evidence: { sample_size: total, success_rate: successRate, computed_at: new Date().toISOString() },
        is_active: isActive,
        last_observed_at: new Date().toISOString(),
      };

      if (existing) {
        updateOps.push({ id: existing.id, payload });
        updated++;
        if (existing.is_active && !isActive) deactivated++;
      } else {
        insertRows.push(payload);
        created++;
      }
    }

    // Batch apply all writes after the loop.
    await Promise.all(updateOps.map((u) => sb.from("ai_memory").update(u.payload).eq("id", u.id)));
    if (insertRows.length > 0) await sb.from("ai_memory").insert(insertRows);

    // ── Auto-revert: для будь-якого щойно деактивованого токсичного патерну
    // знаходимо ще-не-revoke-нуті ai_actions цього (agent, action_type) у статусі applied
    // і ставимо revert (Enforcer + ROI Collector в наступних запусках це підхоплять).
    let auto_reverted = 0;
    if (deactivated > 0) {
      const { data: toxicRules } = await sb
        .from("ai_memory")
        .select("pattern_key")
        .eq("agent", "tribunal")
        .eq("category", "action-effectiveness")
        .eq("is_active", false)
        .gte("last_observed_at", new Date(Date.now() - 7 * 86400_000).toISOString());

      // Parallelize per-rule lookups instead of sequential awaits.
      const stuckResults = await Promise.all(
        (toxicRules ?? []).map(async (rule: { pattern_key: string }) => {
          const parts = rule.pattern_key.split(":");
          if (parts.length < 3) return [] as { id: string }[];
          const agent = parts[1];
          const action_type = parts.slice(2).join(":");
          const { data } = await sb
            .from("ai_actions")
            .select("id")
            .eq("agent_id", agent)
            .eq("action_type", action_type)
            .eq("status", "applied")
            .is("reverted_at", null)
            .gte("applied_at", new Date(Date.now() - 7 * 86400_000).toISOString())
            .limit(50);
          return (data ?? []) as { id: string }[];
        }),
      );

      const allStuckIds = stuckResults.flat().map((r) => r.id);
      if (allStuckIds.length > 0) {
        await sb.from("ai_actions").update({
          status: "reverted",
          reverted_at: new Date().toISOString(),
          reverted_reason: `auto-revert: toxic pattern (success<30%, n≥5)`,
        }).in("id", allStuckIds);
        auto_reverted = allStuckIds.length;
      }
    }

    return {
      result: new Response(JSON.stringify({ ok: true, groups: groups.size, updated, created, deactivated, auto_reverted }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }),
      summary: `groups=${groups.size}, updated=${updated}, created=${created}, deactivated=${deactivated}, reverted=${auto_reverted}`,
      payload: { groups: groups.size, updated, created, deactivated, auto_reverted },
      status: "success",
    };
  }).catch((e) => {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  });
});
