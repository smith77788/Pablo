// Cycle #22 — Loyalty Tier Engine
// Reads loyalty_tiers (sorted by rank desc), assigns the highest tier each
// customer qualifies for based on (total_orders, total_spent, predicted LTV).
// Writes/updates customer_loyalty and emits an aggregated insight.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-loyalty-tier-engine", req, null, async () => {
    const __res = await (async () => {

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const { data: tiers } = await supabase
      .from("loyalty_tiers")
      .select("id, name, rank, min_orders, min_total_spent, min_predicted_ltv, is_active")
      .eq("is_active", true)
      .order("rank", { ascending: false });

    if (!tiers?.length) {
      return new Response(JSON.stringify({ ok: true, tiered: 0, reason: "no_tiers" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const { data: customers } = await supabase
      .from("customers")
      .select("id, total_orders, total_spent")
      .limit(5000);

    const { data: ltvRows } = await supabase
      .from("customer_ltv_scores")
      .select("customer_id, predicted_ltv_12m, computed_at")
      .order("computed_at", { ascending: false })
      .limit(20_000);
    const ltvMap = new Map<string, number>();
    for (const r of ltvRows ?? []) {
      if (!ltvMap.has(r.customer_id)) ltvMap.set(r.customer_id, Number(r.predicted_ltv_12m));
    }

    const tierCounts: Record<string, number> = {};
    const upserts: any[] = [];

    for (const c of customers ?? []) {
      const orders = Number(c.total_orders ?? 0);
      const spent = Number(c.total_spent ?? 0);
      const ltv = ltvMap.get(c.id) ?? spent;
      // Walk tiers from highest rank down; pick first that qualifies
      const matched = tiers.find((t: any) =>
        orders >= (t.min_orders ?? 0) &&
        spent >= Number(t.min_total_spent ?? 0) &&
        ltv >= Number(t.min_predicted_ltv ?? 0),
      );
      if (!matched) continue;
      tierCounts[matched.name] = (tierCounts[matched.name] ?? 0) + 1;
      upserts.push({
        customer_id: c.id,
        tier_id: matched.id,
        tier_name: matched.name,
        computed_at: new Date().toISOString(),
      });
    }

    if (upserts.length) {
      const chunk = 500;
      const chunks: any[][] = [];
      for (let i = 0; i < upserts.length; i += chunk) chunks.push(upserts.slice(i, i + chunk));
      await Promise.all(
        chunks.map((c) => supabase.from("customer_loyalty").upsert(c, { onConflict: "customer_id" } as any)),
      );
    }

    const summary = Object.entries(tierCounts).map(([n, c]) => `${n}: ${c}`).join(", ");
    if (upserts.length) {
      await supabase.from("ai_insights").insert({
        insight_type: "loyalty_distribution",
        title: `Loyalty: ${upserts.length} клієнтів отримали тір`,
        description: `Розподіл: ${summary || "—"}. Тіри визначаються кількістю замовлень, сумою витрат та прогнозом LTV.`,
        confidence: 0.7,
        risk_level: "low",
        affected_layer: "crm",
        metrics: { distribution: tierCounts, total_tiered: upserts.length },
      });
    }

    return new Response(JSON.stringify({ ok: true, tiered: upserts.length, distribution: tierCounts }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("loyalty-tier-engine error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
