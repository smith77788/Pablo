// Cycle #11 — Predictive LTV Engine
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

  return runAgent("acos-ltv-predictor", req, null, async () => {
    const __res = await (async () => {

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // Pull all customers with order data
    const { data: customers, error: cErr } = await supabase
      .from("customers")
      .select("id, total_orders, total_spent, created_at, lifecycle_stage")
      .limit(5000);
    if (cErr) throw cErr;

    const now = Date.now();
    const upserts: any[] = [];
    let highValue = 0;

    for (const c of customers ?? []) {
      const ageDays = Math.max(1, (now - new Date(c.created_at).getTime()) / 86_400_000);
      const aov = c.total_orders > 0 ? c.total_spent / c.total_orders : 0;
      const orderRate = c.total_orders / ageDays; // orders/day so far
      // Project 365 days, dampen if very young account
      const maturity = Math.min(1, ageDays / 60);
      const projectedOrders12m = orderRate * 365 * (0.5 + 0.5 * maturity);
      const predictedLtv = Math.max(c.total_spent, Math.round(aov * projectedOrders12m));
      const confidence = Math.min(0.95, 0.3 + 0.1 * c.total_orders + 0.2 * maturity);

      let segment = "unknown";
      if (c.total_orders === 0) segment = "lead";
      else if (predictedLtv >= 5000) segment = "vip";
      else if (predictedLtv >= 2000) segment = "high";
      else if (predictedLtv >= 800) segment = "mid";
      else segment = "low";

      if (segment === "vip" || segment === "high") highValue++;

      upserts.push({
        customer_id: c.id,
        predicted_ltv_12m: predictedLtv,
        observed_ltv: c.total_spent,
        confidence,
        segment,
        features: { aov, order_rate_per_day: orderRate, age_days: Math.round(ageDays) },
        computed_at: new Date().toISOString(),
      });
    }

    // Batch insert (fresh snapshot rows)
    if (upserts.length) {
      const chunk = 500;
      const chunks: any[][] = [];
      for (let i = 0; i < upserts.length; i += chunk) chunks.push(upserts.slice(i, i + chunk));
      await Promise.all(chunks.map((c) => supabase.from("customer_ltv_scores").insert(c)));
    }

    // Insight: high-value share
    const total = upserts.length || 1;
    const highValuePct = Math.round((highValue / total) * 100);
    await supabase.from("ai_insights").insert({
      insight_type: "ltv_distribution",
      title: `LTV: ${highValue} клієнтів у high/vip-сегменті (${highValuePct}%)`,
      description: `Прогноз 12-міс LTV побудовано для ${total} клієнтів. ${highValue} оцінено як high+vip — фокус ретеншн-кампаній на них.`,
      confidence: 0.7,
      risk_level: "low",
      affected_layer: "crm",
      metrics: { total, highValue, highValuePct },
    });

    return new Response(JSON.stringify({ ok: true, scored: total, highValue }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("ltv-predictor error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
