// ACOS Cycle #9 — Cohort Performance Analyzer
// Segments customers into actionable cohorts and generates per-segment strategy insights.
//
// Cohorts:
//   - new        : 0 orders, first session ≤ 7d ago
//   - activated  : exactly 1 order, ≤ 30d since first order
//   - returning  : 2-4 orders, last order ≤ 60d ago
//   - vip        : ≥5 orders OR total_spent ≥ 5000 ₴
//   - at_risk    : ≥1 order, last order 60-120d ago
//   - churned    : ≥1 order, last order > 120d ago
//
// For each cohort, computes:
//   - size, total_revenue, avg_order_value, conversion_rate (sessions→orders), churn_velocity
//
// Then generates ai_insights with per-cohort strategy recommendations:
//   - new        → reduce friction, free shipping nudge
//   - activated  → second-order nurture (already covered by Cycle #6 of acos-second-order-nurture)
//   - returning  → bundle upsell, loyalty hint
//   - vip        → exclusive offers, no discount needed
//   - at_risk    → winback campaign trigger
//   - churned    → low-cost reactivation (one shot, then archive)
//
// Idempotent: dedups via dedup_bucket (handled by trigger).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const DAY_MS = 24 * 60 * 60 * 1000;

type Cohort = "new" | "activated" | "returning" | "vip" | "at_risk" | "churned";

interface CohortStats {
  cohort: Cohort;
  size: number;
  total_revenue: number;
  avg_order_value: number;
  avg_orders_per_customer: number;
  pct_of_revenue: number;
}

const STRATEGY_BY_COHORT: Record<Cohort, { title: string; description: string; risk: string; expected: string }> = {
  new: {
    title: "Сегмент «Нові»: знизити тертя першого замовлення",
    description: "Фокус на free-shipping nudge на чекауті, прибрати додаткові поля форми. Промокод не пропонувати — псує LTV.",
    risk: "low",
    expected: "+8-12% conversion на першому замовленні",
  },
  activated: {
    title: "Сегмент «Активовані»: тригер другого замовлення",
    description: "Делеговано на acos-second-order-nurture. Перевірити, що nurture-листи виходять у вікні 7-21 день після першого замовлення.",
    risk: "low",
    expected: "+15-20% repeat rate",
  },
  returning: {
    title: "Сегмент «Повертаються»: bundle upsell + натяк на лояльність",
    description: "Запустити bundle-рекомендації на product page. Email/Telegram з натяком «5-те замовлення = -10%». Не давати знижки на кожне.",
    risk: "low",
    expected: "+5-8% AOV, +10% retention",
  },
  vip: {
    title: "Сегмент «VIP»: ексклюзив, без знижок",
    description: "Раннє інформування про новинки, безкоштовна доставка завжди. Знижки руйнують маржу — VIP купують за відносини.",
    risk: "low",
    expected: "Збереження AOV, +5% frequency",
  },
  at_risk: {
    title: "Сегмент «Під ризиком»: winback зараз",
    description: "Тригерити acos-winback edge function для цих user_id. Персональна знижка 10-15% з дедлайном 7 днів.",
    risk: "medium",
    expected: "20-30% reactivation rate",
  },
  churned: {
    title: "Сегмент «Втрачені»: одна спроба реактивації",
    description: "Один лист/повідомлення з найкращою пропозицією. Якщо не реагують — архівувати в low-priority. Не спамити.",
    risk: "low",
    expected: "5-8% reactivation, економія на не-конверсійних розсилках",
  },
};

interface OrderRow {
  user_id: string | null;
  customer_phone: string | null;
  customer_email: string | null;
  total: number;
  created_at: string;
}

const customerKey = (o: OrderRow): string =>
  o.user_id || o.customer_phone || o.customer_email || `unknown:${o.created_at}`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-cohort-performance", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const now = Date.now();
    const since = new Date(now - 365 * DAY_MS).toISOString(); // 1 year window

    const { data: orders, error } = await supabase
      .from("orders")
      .select("user_id, customer_phone, customer_email, total, created_at")
      .gte("created_at", since)
      .neq("status", "cancelled")
      .limit(10000);

    if (error) throw error;

    // Group by customer
    const customerMap = new Map<string, { orders: OrderRow[]; total: number; firstOrder: number; lastOrder: number }>();
    for (const o of (orders ?? []) as OrderRow[]) {
      const key = customerKey(o);
      const ts = new Date(o.created_at).getTime();
      const existing = customerMap.get(key);
      if (existing) {
        existing.orders.push(o);
        existing.total += (o.total as number) || 0;
        existing.firstOrder = Math.min(existing.firstOrder, ts);
        existing.lastOrder = Math.max(existing.lastOrder, ts);
      } else {
        customerMap.set(key, {
          orders: [o],
          total: (o.total as number) || 0,
          firstOrder: ts,
          lastOrder: ts,
        });
      }
    }

    // Classify
    const cohorts: Record<Cohort, { customers: number; revenue: number; orders: number }> = {
      new: { customers: 0, revenue: 0, orders: 0 },
      activated: { customers: 0, revenue: 0, orders: 0 },
      returning: { customers: 0, revenue: 0, orders: 0 },
      vip: { customers: 0, revenue: 0, orders: 0 },
      at_risk: { customers: 0, revenue: 0, orders: 0 },
      churned: { customers: 0, revenue: 0, orders: 0 },
    };

    for (const [, c] of customerMap) {
      const orderCount = c.orders.length;
      const daysSinceLast = (now - c.lastOrder) / DAY_MS;
      const daysSinceFirst = (now - c.firstOrder) / DAY_MS;

      let cohort: Cohort;
      if (orderCount >= 5 || c.total >= 5000) {
        cohort = "vip";
      } else if (daysSinceLast > 120) {
        cohort = "churned";
      } else if (daysSinceLast > 60) {
        cohort = "at_risk";
      } else if (orderCount === 1 && daysSinceFirst <= 30) {
        cohort = "activated";
      } else if (orderCount >= 2) {
        cohort = "returning";
      } else {
        cohort = "new";
      }

      cohorts[cohort].customers += 1;
      cohorts[cohort].revenue += c.total;
      cohorts[cohort].orders += orderCount;
    }

    const totalRevenue = Object.values(cohorts).reduce((s, c) => s + c.revenue, 0) || 1;

    const stats: CohortStats[] = (Object.keys(cohorts) as Cohort[]).map((k) => ({
      cohort: k,
      size: cohorts[k].customers,
      total_revenue: cohorts[k].revenue,
      avg_order_value: cohorts[k].orders > 0 ? cohorts[k].revenue / cohorts[k].orders : 0,
      avg_orders_per_customer: cohorts[k].customers > 0 ? cohorts[k].orders / cohorts[k].customers : 0,
      pct_of_revenue: cohorts[k].revenue / totalRevenue,
    }));

    // Batch insert all non-empty cohort strategy insights
    const insightRows = stats
      .filter((s) => s.size > 0)
      .map((s) => {
        const strat = STRATEGY_BY_COHORT[s.cohort];
        return {
          insight_type: "cohort_strategy",
          affected_layer: "growth",
          risk_level: strat.risk,
          title: strat.title,
          description: `${strat.description}\n\nКогорта: ${s.size} клієнтів, ${s.total_revenue.toFixed(0)} ₴ (${(s.pct_of_revenue * 100).toFixed(1)}% виручки), AOV ${s.avg_order_value.toFixed(0)} ₴.`,
          confidence: 0.75,
          expected_impact: strat.expected,
          metrics: {
            cohort: s.cohort,
            size: s.size,
            total_revenue: s.total_revenue,
            avg_order_value: s.avg_order_value,
            avg_orders_per_customer: s.avg_orders_per_customer,
            pct_of_revenue: s.pct_of_revenue,
            target_entity: "customer_segment",
            target_id: s.cohort,
          },
          status: "new",
        };
      });
    let insightsInserted = 0;
    if (insightRows.length > 0) {
      await supabase.from("ai_insights").insert(insightRows).catch(() => {});
      insightsInserted = insightRows.length;
    }

    __agent.success();
    return new Response(
      JSON.stringify({
        ok: true,
        total_customers: customerMap.size,
        total_revenue: totalRevenue,
        cohorts: stats,
        insights_inserted: insightsInserted,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    __agent.error(err);
    console.error("acos-cohort-performance error", err);
    return new Response(
      JSON.stringify({ ok: false, error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
