// ACOS Promo Fatigue Detector
// Why: we fire winback / cart-recovery / auto-promo at customers, but if
// a customer gets 3+ promo touches in 30 days WITHOUT ordering, they're
// fatigued — sending more hurts brand perception and Telegram block rate.
// This function flags those customers and recommends pausing them.
//
// Method:
//   1. Pull all promo-bearing events from last 30 days:
//      winback_sent, cart_recovery_sent, auto_promo_sent.
//   2. Group by customer_id; count touches.
//   3. For each customer with 3+ touches, check if they ordered after
//      the first touch (orders.customer_phone match within window).
//   4. Customers with ≥3 touches and 0 orders → fatigued.
//
// Output: single rolling `promo_fatigue` insight + per-customer list in metrics.
//
// Schedule: weekly (Friday 12:00 UTC). Idempotent.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const LOOKBACK_DAYS = 30;
const FATIGUE_THRESHOLD = 3; // touches without conversion
const PROMO_EVENT_TYPES = ["winback_sent", "cart_recovery_sent", "auto_promo_sent"];

interface PromoEventMeta {
  customer_id?: string;
  promo_code?: string;
  success?: boolean;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-promo-fatigue-detector", req);

  try {
    const cutoffIso = new Date(Date.now() - LOOKBACK_DAYS * 86400_000).toISOString();

    // 1. All promo touches in window.
    const { data: events, error: eErr } = await supabase
      .from("events")
      .select("event_type, metadata, created_at")
      .in("event_type", PROMO_EVENT_TYPES)
      .gte("created_at", cutoffIso)
      .limit(2000);
    if (eErr) throw eErr;

    if (!events || events.length === 0) {
      return new Response(
        JSON.stringify({ ok: true, fatigued: 0, reason: "no promo events in window" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // 2. Group by customer_id.
    type Touch = { event_type: string; created_at: string; promo_code?: string };
    const byCustomer = new Map<string, Touch[]>();
    for (const e of events) {
      const meta = (e.metadata ?? {}) as PromoEventMeta;
      // only count successful sends
      if (meta.success === false) continue;
      const cid = meta.customer_id;
      if (!cid) continue;
      const arr = byCustomer.get(cid) ?? [];
      arr.push({
        event_type: e.event_type,
        created_at: e.created_at,
        promo_code: meta.promo_code,
      });
      byCustomer.set(cid, arr);
    }

    // 3. Filter to candidates with ≥ threshold touches.
    const candidates = Array.from(byCustomer.entries()).filter(
      ([, touches]) => touches.length >= FATIGUE_THRESHOLD
    );

    if (candidates.length === 0) {
      // overwrite/clear stale insight
      await supabase.from("ai_insights").delete().eq("insight_type", "promo_fatigue");
      return new Response(
        JSON.stringify({
          ok: true,
          fatigued: 0,
          touched_customers: byCustomer.size,
          reason: "no customers above threshold",
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // 4. For each candidate, check if they have an order after their FIRST touch.
    const customerIds = candidates.map(([cid]) => cid);
    const { data: customers } = await supabase
      .from("customers")
      .select("id, name, phone, email, telegram_chat_id, total_orders, total_spent")
      .in("id", customerIds);
    const customerById = new Map((customers ?? []).map((c) => [c.id, c]));

    // Pull orders within window for these customers (match by phone or email).
    const phones = (customers ?? []).map((c) => c.phone).filter(Boolean) as string[];
    const emails = (customers ?? []).map((c) => c.email).filter(Boolean) as string[];

    const [phoneOrdersRes, emailOrdersRes] = await Promise.all([
      phones.length > 0
        ? supabase.from("orders").select("customer_phone, customer_email, created_at")
            .in("customer_phone", phones).gte("created_at", cutoffIso)
        : Promise.resolve({ data: [] as any[] }),
      emails.length > 0
        ? supabase.from("orders").select("customer_phone, customer_email, created_at")
            .in("customer_email", emails).gte("created_at", cutoffIso)
        : Promise.resolve({ data: [] as any[] }),
    ]);
    const ordersInWindow: Array<{ customer_phone: string | null; customer_email: string | null; created_at: string }> = [
      ...(phoneOrdersRes.data ?? []),
      ...(emailOrdersRes.data ?? []),
    ];

    interface FatiguedRow {
      customer_id: string;
      name: string;
      phone: string | null;
      touches: number;
      first_touch_at: string;
      last_touch_at: string;
      promo_codes: string[];
      total_orders: number;
      severity: "high" | "medium";
    }

    const fatigued: FatiguedRow[] = [];

    for (const [cid, touches] of candidates) {
      const c = customerById.get(cid);
      if (!c) continue;
      const sorted = touches.sort(
        (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
      );
      const firstAt = sorted[0].created_at;

      // Did they order after the first touch?
      const ordered = ordersInWindow.some((o) => {
        const matches =
          (c.phone && o.customer_phone === c.phone) ||
          (c.email && o.customer_email === c.email);
        return matches && new Date(o.created_at).getTime() >= new Date(firstAt).getTime();
      });
      if (ordered) continue;

      fatigued.push({
        customer_id: cid,
        name: c.name,
        phone: c.phone,
        touches: touches.length,
        first_touch_at: firstAt,
        last_touch_at: sorted[sorted.length - 1].created_at,
        promo_codes: Array.from(
          new Set(sorted.map((t) => t.promo_code).filter(Boolean) as string[])
        ).slice(0, 5),
        total_orders: c.total_orders,
        severity: touches.length >= 5 ? "high" : "medium",
      });
    }

    // Sort: most touches first
    fatigued.sort((a, b) => b.touches - a.touches);

    // 5. Replace previous insight.
    await supabase.from("ai_insights").delete().eq("insight_type", "promo_fatigue");

    if (fatigued.length === 0) {
      return new Response(
        JSON.stringify({
          ok: true,
          fatigued: 0,
          touched_customers: byCustomer.size,
          candidates: candidates.length,
          reason: "all candidates converted",
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const highSeverity = fatigued.filter((f) => f.severity === "high").length;
    const totalTouches = fatigued.reduce((s, f) => s + f.touches, 0);
    const wastedCost = totalTouches * 5; // matches winback cost-per-send

    const verdict =
      highSeverity >= 5
        ? "Промо-кампанії перенасичені — паузу 14 днів для high-severity клієнтів."
        : fatigued.length >= 10
        ? "Помірна fatigue — переглянь частоту winback (збільш SILENT_DAYS до 45)."
        : "Низький рівень fatigue — система працює нормально, продовжуй моніторинг.";

    await supabase.from("ai_insights").insert({
      insight_type: "promo_fatigue",
      title: `Promo fatigue: ${fatigued.length} клієнтів отримали ${FATIGUE_THRESHOLD}+ промо без покупки`,
      description:
        `За ${LOOKBACK_DAYS} днів виявлено ${fatigued.length} fatigued клієнтів:\n` +
        `🔴 ${highSeverity} high-severity (5+ touches)\n` +
        `🟡 ${fatigued.length - highSeverity} medium-severity (3-4 touches)\n` +
        `💸 ~${wastedCost}₴ витрачено на ${totalTouches} touches без ROI\n\n` +
        verdict,
      expected_impact:
        highSeverity >= 5
          ? `Пауза заощадить ~${Math.round(highSeverity * 5 * 4)}₴/міс і зменшить TG block rate.`
          : `Оптимізація частоти може заощадити ~${Math.round(fatigued.length * 3)}₴/тиждень.`,
      confidence: fatigued.length >= 5 ? 0.8 : 0.5,
      risk_level: highSeverity >= 5 ? "high" : fatigued.length >= 10 ? "medium" : "low",
      affected_layer: "bot",
      metrics: {
        fatigued_count: fatigued.length,
        high_severity: highSeverity,
        medium_severity: fatigued.length - highSeverity,
        total_touches: totalTouches,
        wasted_cost_uah: wastedCost,
        touched_customers: byCustomer.size,
        candidates: candidates.length,
        lookback_days: LOOKBACK_DAYS,
        threshold: FATIGUE_THRESHOLD,
        fatigued_list: fatigued.slice(0, 20), // top 20 for UI
      },
    });

    __agent.success();
    return new Response(
      JSON.stringify({
        ok: true,
        fatigued: fatigued.length,
        high_severity: highSeverity,
        wasted_cost_uah: wastedCost,
        verdict,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    __agent.error(err);
    console.error("[acos-promo-fatigue-detector]", err);
    return new Response(JSON.stringify({ ok: false, error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
