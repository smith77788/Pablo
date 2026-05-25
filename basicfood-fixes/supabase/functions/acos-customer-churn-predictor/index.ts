// ACOS Customer Churn Predictor — for every VIP customer (5+ orders),
// computes their personal average inter-order interval and flags them
// as "at risk" when current recency exceeds 1.5× their personal average.
// Tags at-risk customers with `churn_risk:<YYYY-MM-DD>` so the winback
// engine can prioritize them, and emits a single roll-up insight.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const VIP_MIN_ORDERS = 5;
const RISK_MULTIPLIER = 1.5;
const TAG_PREFIX = "churn_risk:";
const MAX_TAG_AGE_DAYS = 14; // refresh tag every 14 days

interface OrderRow {
  customer_phone: string | null;
  customer_email: string | null;
  total: number;
  created_at: string;
}

interface CustomerRow {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  total_orders: number;
  total_spent: number;
  tags: string[];
}

interface AtRiskCustomer {
  customer_id: string;
  name: string;
  total_orders: number;
  avg_interval_days: number;
  current_recency_days: number;
  overdue_ratio: number;
  expected_ltv_at_risk: number;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-customer-churn-predictor", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    // 1. Pull all VIP customers (5+ orders).
    const { data: customers } = await supabase
      .from("customers")
      .select("id, name, phone, email, total_orders, total_spent, tags")
      .gte("total_orders", VIP_MIN_ORDERS)
      .limit(2000);

    const vips = (customers ?? []) as CustomerRow[];
    if (vips.length === 0) {
      return new Response(
        JSON.stringify({ vips: 0, at_risk: 0, reason: "no_vips" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull all orders we need to compute inter-order intervals.
    // (Limit to last 730 days to bound the query.)
    const since = new Date(Date.now() - 730 * 24 * 60 * 60 * 1000).toISOString();
    const { data: orderRows } = await supabase
      .from("orders")
      .select("customer_phone, customer_email, total, created_at")
      .gte("created_at", since)
      .neq("source", "spin_game")
      .order("created_at", { ascending: true });

    // Index orders by phone and email for fast lookup.
    const ordersByPhone = new Map<string, OrderRow[]>();
    const ordersByEmail = new Map<string, OrderRow[]>();
    for (const o of (orderRows ?? []) as OrderRow[]) {
      if (o.customer_phone) {
        const arr = ordersByPhone.get(o.customer_phone) ?? [];
        arr.push(o);
        ordersByPhone.set(o.customer_phone, arr);
      }
      if (o.customer_email) {
        const arr = ordersByEmail.get(o.customer_email) ?? [];
        arr.push(o);
        ordersByEmail.set(o.customer_email, arr);
      }
    }

    const now = Date.now();
    const today = new Date().toISOString().slice(0, 10);
    const tagCutoff = now - MAX_TAG_AGE_DAYS * 24 * 60 * 60 * 1000;

    const atRisk: AtRiskCustomer[] = [];
    let stillHealthy = 0;
    let tagsApplied = 0;
    let tagsCleared = 0;

    // Collect all tag updates in-memory, then apply in parallel at the end.
    const tagUpdates: Array<{ id: string; tags: string[] }> = [];

    for (const cust of vips) {
      // Resolve this customer's order history (prefer phone, fallback email).
      let history: OrderRow[] = [];
      if (cust.phone) history = ordersByPhone.get(cust.phone) ?? [];
      if (history.length < VIP_MIN_ORDERS && cust.email) {
        history = ordersByEmail.get(cust.email) ?? [];
      }
      // Need at least N orders to compute meaningful intervals.
      if (history.length < VIP_MIN_ORDERS) continue;

      // Compute inter-order intervals (already sorted ascending).
      const intervals: number[] = [];
      for (let i = 1; i < history.length; i++) {
        const a = new Date(history[i - 1].created_at).getTime();
        const b = new Date(history[i].created_at).getTime();
        intervals.push((b - a) / (24 * 60 * 60 * 1000));
      }
      if (intervals.length === 0) continue;

      const avgInterval = intervals.reduce((s, x) => s + x, 0) / intervals.length;
      // Skip super-low-frequency customers (avoid false positives on yearly buyers).
      if (avgInterval < 3) continue;

      const lastOrderAt = new Date(history[history.length - 1].created_at).getTime();
      const recency = (now - lastOrderAt) / (24 * 60 * 60 * 1000);
      const overdueRatio = recency / avgInterval;

      const existingTag = cust.tags.find((t) => t.startsWith(TAG_PREFIX));
      const existingTs = existingTag
        ? new Date(existingTag.split(":")[1]).getTime()
        : 0;
      const tagIsFresh = !isNaN(existingTs) && existingTs >= tagCutoff;

      if (overdueRatio >= RISK_MULTIPLIER) {
        // At risk → tag if not already fresh.
        if (!tagIsFresh) {
          const nextTags = [
            ...cust.tags.filter((t) => !t.startsWith(TAG_PREFIX)),
            `${TAG_PREFIX}${today}`,
          ];
          tagUpdates.push({ id: cust.id, tags: nextTags });
          tagsApplied++;
        }
        // Estimate "at risk" LTV: average order value × expected remaining orders
        // until natural churn (assume 3× avg interval window).
        const aov = cust.total_spent / Math.max(1, cust.total_orders);
        const expectedRemaining = 3; // gross simplification
        atRisk.push({
          customer_id: cust.id,
          name: cust.name,
          total_orders: cust.total_orders,
          avg_interval_days: Math.round(avgInterval),
          current_recency_days: Math.round(recency),
          overdue_ratio: Math.round(overdueRatio * 100) / 100,
          expected_ltv_at_risk: Math.round(aov * expectedRemaining),
        });
      } else {
        stillHealthy++;
        // Clear stale risk tag if customer recovered.
        if (existingTag) {
          const nextTags = cust.tags.filter((t) => !t.startsWith(TAG_PREFIX));
          tagUpdates.push({ id: cust.id, tags: nextTags });
          tagsCleared++;
        }
      }
    }

    // Apply all tag updates in parallel.
    if (tagUpdates.length > 0) {
      await Promise.all(
        tagUpdates.map(({ id, tags }) =>
          supabase.from("customers").update({ tags }).eq("id", id).catch(() => {}),
        ),
      );
    }

    // Roll-up insight when there's meaningful at-risk volume.
    if (atRisk.length >= 3) {
      const totalLtvAtRisk = atRisk.reduce((s, a) => s + a.expected_ltv_at_risk, 0);
      const top = atRisk
        .slice()
        .sort((a, b) => b.expected_ltv_at_risk - a.expected_ltv_at_risk)
        .slice(0, 5);
      await supabase.from("ai_insights").insert({
        insight_type: "vip_churn_risk",
        title: `${atRisk.length} VIP клієнтів на грані відтоку (~${totalLtvAtRisk.toLocaleString()}₴ LTV)`,
        description: `Виявлено ${atRisk.length} VIP клієнтів (≥${VIP_MIN_ORDERS} замовлень) у яких поточна recency перевищує особистий середній інтервал у ${RISK_MULTIPLIER}+ рази. Усім додано тег churn_risk — Winback engine відправить персональний touch. Топ за LTV: ${top.map((t) => t.name).join(", ")}.`,
        expected_impact: `Збереження ~${Math.round(totalLtvAtRisk * 0.2).toLocaleString()}₴ (20% recovery rate)`,
        confidence: 0.7,
        risk_level: "low",
        affected_layer: "telegram_bot",
        status: "new",
        metrics: {
          vips_total: vips.length,
          at_risk_count: atRisk.length,
          healthy: stillHealthy,
          tags_applied: tagsApplied,
          tags_cleared: tagsCleared,
          total_ltv_at_risk: totalLtvAtRisk,
          top_5: top,
        },
      });
    }

    return new Response(
      JSON.stringify({
        vips: vips.length,
        at_risk: atRisk.length,
        healthy: stillHealthy,
        tags_applied: tagsApplied,
        tags_cleared: tagsCleared,
        sample: atRisk.slice(0, 10),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
    })();
    return { response: __res };
  });
});
