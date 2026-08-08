/**
 * Outreach ROI Collector — JOIN events (utm_campaign LIKE 'outreach_%')
 * + orders.promo_code LIKE 'OUT%' → пише в outreach_metrics на action.
 *
 * Запуск: cron щодня (або вручну).
 */
import { corsHeaders, svcClient } from "../_shared/outreach.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

interface Action {
  id: string;
  lead_id: string;
  channel: string;
  utm_campaign: string;
  promo_code: string | null;
  posted_at: string | null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;
  const body = await req.json().catch(() => ({}));
  const trigger = detectTrigger(req, body);
  try {
    const sb = svcClient();

    // Беремо всі approved/posted дії (навіть якщо ще не опубліковані —
    // їх UTM можуть з'являтися, якщо ми поставили посилання деінде).
    const { data: actions, error } = await sb
      .from("outreach_actions")
      .select("id, lead_id, channel, utm_campaign, promo_code, posted_at")
      .in("status", ["posted", "approved"])
      .limit(1000);
    if (error) throw new Error(error.message);

    const stats = { actions: 0, metrics_upserted: 0 };

    for (const a of (actions ?? []) as Action[]) {
      stats.actions++;

      // Збираємо events за utm_campaign
      const { data: ev } = await sb
        .from("events")
        .select("event_type")
        .ilike("url", `%utm_campaign=${a.utm_campaign}%`)
        .limit(5000);

      let visits = 0, add_to_cart = 0;
      for (const e of ev ?? []) {
        if (e.event_type === "page_view" || e.event_type === "product_view") visits++;
        if (e.event_type === "add_to_cart") add_to_cart++;
      }

      let orders_count = 0, revenue = 0;
      if (a.promo_code) {
        // 1) Знаходимо id промокоду
        const { data: pc } = await sb
          .from("promo_codes").select("id").eq("code", a.promo_code).maybeSingle();
        if (pc?.id) {
          const { data: ord } = await sb
            .from("orders")
            .select("id, total, status")
            .eq("promo_code_id", pc.id)
            .neq("status", "cancelled");
          orders_count = ord?.length ?? 0;
          // total зберігається в копійках (integer) → переводимо в гривні
          revenue = (ord ?? []).reduce((s: number, o: any) => s + (Number(o.total) || 0), 0) / 100;
        }
      }

      const ctr = visits > 0 ? +(orders_count / visits).toFixed(4) : 0;
      const conversion_rate = visits > 0 ? +(orders_count / Math.max(visits, 1)).toFixed(4) : 0;
      const roi_per_action = revenue; // витрат на outreach 0 → ROI = виторг

      await sb.from("outreach_metrics").upsert({
        action_id: a.id,
        lead_id: a.lead_id,
        channel: a.channel,
        utm_campaign: a.utm_campaign,
        impressions: 0,
        clicks: visits,
        visits,
        add_to_cart,
        orders_count,
        revenue,
        ctr,
        conversion_rate,
        roi_per_action,
        computed_at: new Date().toISOString(),
      }, { onConflict: "action_id" });
      stats.metrics_upserted++;
    }

    try {
      await sb.from("agent_runs").insert({
        function_name: "outreach-roi-collector",
        trigger,
        status: "success",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `actions=${stats.actions}, metrics_upserted=${stats.metrics_upserted}`,
        payload: { stats },
      });
    } catch { /* ignore */ }

    return new Response(JSON.stringify({ ok: true, stats }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("[roi] fatal:", e);
    try {
      await svcClient().from("agent_runs").insert({
        function_name: "outreach-roi-collector",
        trigger,
        status: "error",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        error_message: String(e?.message ?? e).slice(0, 2000),
      });
    } catch { /* ignore */ }
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
