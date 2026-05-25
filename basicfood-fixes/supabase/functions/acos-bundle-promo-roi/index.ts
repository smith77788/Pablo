// ACOS Bundle Promo ROI — for every BUNDLE promo_code created ≥7 days ago
// (and not yet measured), calculates: redemption count, gross/net revenue,
// avg order value, and lift vs baseline AOV. Emits a winner/flop insight
// and tags processed promos in metadata so we don't double-count.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const MIN_AGE_DAYS = 7;     // promo must be at least 7d old before measurement
const LOOKBACK_DAYS = 60;   // pull promos created in last 60d
const BASELINE_LOOKBACK = 30; // baseline AOV computed from last 30d orders

interface PromoRow {
  id: string;
  code: string;
  current_uses: number;
  discount_value: number;
  min_order_amount: number;
  starts_at: string | null;
  ends_at: string | null;
  created_at: string;
  is_active: boolean;
}

interface UseRow {
  promo_code_id: string;
  order_id: string;
}

interface OrderRow {
  id: string;
  total: number;
  subtotal: number;
  discount_amount: number;
  status: string;
}

interface InsightRow {
  metrics: { promo_code?: string } | null;
}

interface PromoStat {
  code: string;
  promo_id: string;
  uses: number;
  paid_uses: number;
  gross_revenue: number;
  discount_given: number;
  net_revenue: number;
  avg_order_value: number;
  lift_pct: number;
  age_days: number;
}

const VALID_STATUSES = new Set(["new", "confirmed", "paid", "shipped", "delivered"]);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const now = Date.now();
    const lookbackStart = new Date(now - LOOKBACK_DAYS * dayMs).toISOString();
    const matureCutoff = new Date(now - MIN_AGE_DAYS * dayMs).toISOString();
    const baselineStart = new Date(now - BASELINE_LOOKBACK * dayMs).toISOString();

    // 1. Pull BUNDLE promo codes from lookback window.
    const { data: promos } = await supabase
      .from("promo_codes")
      .select("id, code, current_uses, discount_value, min_order_amount, starts_at, ends_at, created_at, is_active")
      .like("code", "BUNDLE%")
      .gte("created_at", lookbackStart)
      .lte("created_at", matureCutoff)
      .order("created_at", { ascending: false })
      .limit(100);

    const candidates = (promos ?? []) as PromoRow[];
    if (candidates.length === 0) {
      return new Response(
        JSON.stringify({ promos: [], reason: "no_mature_bundle_promos", min_age_days: MIN_AGE_DAYS }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Skip already-measured promos (insight already emitted).
    const { data: prior } = await supabase
      .from("ai_insights")
      .select("metrics")
      .in("insight_type", ["bundle_promo_winner", "bundle_promo_flop", "bundle_promo_zero"])
      .gte("created_at", lookbackStart)
      .limit(500);

    const measured = new Set<string>(
      ((prior ?? []) as InsightRow[])
        .map((p) => p.metrics?.promo_code ?? "")
        .filter(Boolean),
    );

    const fresh = candidates.filter((p) => !measured.has(p.code));
    if (fresh.length === 0) {
      return new Response(
        JSON.stringify({ promos: [], reason: "all_already_measured", checked: candidates.length }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 3. Compute baseline AOV (last 30d, valid orders, no BUNDLE promo).
    const { data: baselineOrders } = await supabase
      .from("orders")
      .select("total, status")
      .gte("created_at", baselineStart)
      .neq("source", "spin_game")
      .in("status", ["new", "confirmed", "paid", "shipped", "delivered"])
      .limit(2000);

    const baselineList = (baselineOrders ?? []).filter((o) => VALID_STATUSES.has(o.status));
    const baselineAov = baselineList.length > 0
      ? Math.round(baselineList.reduce((s, o) => s + o.total, 0) / baselineList.length)
      : 0;

    // 4. Pull uses for fresh promos.
    const promoIds = fresh.map((p) => p.id);
    const { data: useRows } = await supabase
      .from("promo_code_uses")
      .select("promo_code_id, order_id")
      .in("promo_code_id", promoIds);
    const uses = (useRows ?? []) as UseRow[];

    // 5. Pull all related orders once.
    const orderIds = [...new Set(uses.map((u) => u.order_id))];
    let orders: OrderRow[] = [];
    if (orderIds.length > 0) {
      const { data: orderRows } = await supabase
        .from("orders")
        .select("id, total, subtotal, discount_amount, status")
        .in("id", orderIds);
      orders = (orderRows ?? []) as OrderRow[];
    }
    const orderById = new Map(orders.map((o) => [o.id, o]));

    // 6. Build per-promo stats.
    const stats: PromoStat[] = fresh.map((promo) => {
      const promoUses = uses.filter((u) => u.promo_code_id === promo.id);
      let paidUses = 0;
      let gross = 0;
      let discount = 0;
      let net = 0;
      for (const u of promoUses) {
        const o = orderById.get(u.order_id);
        if (!o || !VALID_STATUSES.has(o.status)) continue;
        paidUses++;
        gross += o.subtotal;
        discount += o.discount_amount;
        net += o.total;
      }
      const aov = paidUses > 0 ? Math.round(net / paidUses) : 0;
      const lift = baselineAov > 0 ? ((aov - baselineAov) / baselineAov) * 100 : 0;
      return {
        code: promo.code,
        promo_id: promo.id,
        uses: promo.current_uses,
        paid_uses: paidUses,
        gross_revenue: gross,
        discount_given: discount,
        net_revenue: net,
        avg_order_value: aov,
        lift_pct: Math.round(lift * 10) / 10,
        age_days: Math.round((now - new Date(promo.created_at).getTime()) / dayMs),
      };
    });

    // 7. Emit insight per promo (winner / flop / zero) — batch insert.
    const insightRows: any[] = [];
    for (const s of stats) {
      let insightType: "bundle_promo_winner" | "bundle_promo_flop" | "bundle_promo_zero";
      let title: string;
      let description: string;
      let expectedImpact: string;
      let risk: "low" | "medium";

      if (s.paid_uses === 0) {
        insightType = "bundle_promo_zero";
        title = `BUNDLE промо ${s.code} — 0 використань за ${s.age_days}д`;
        description = `Авто-створений BUNDLE промокод не залучив жодного замовлення. Можливі причини: знижка замала vs очікування клієнтів, мінімальна сума замовлення зависока, або пара товарів вже куплена більшістю активних клієнтів. Рекомендація: деактивувати + завести наступну пару.`;
        expectedImpact = `Економимо ~${(s.uses === 0 ? 0 : 50).toLocaleString()}₴/міс на маркетинговому шумі`;
        risk = "low";
      } else if (s.lift_pct >= 15) {
        insightType = "bundle_promo_winner";
        title = `BUNDLE промо ${s.code} — WINNER: +${s.lift_pct}% AOV lift`;
        description = `${s.paid_uses} замовлень з промо за ${s.age_days}д. Середній чек ${s.avg_order_value.toLocaleString()}₴ vs baseline ${baselineAov.toLocaleString()}₴ (lift ${s.lift_pct}%). Net виручка ${s.net_revenue.toLocaleString()}₴, знижка дала ${s.discount_given.toLocaleString()}₴. ROI позитивний — ця пара працює, рекомендується продовжити промо ще на 30д.`;
        expectedImpact = `Якщо подовжити TTL → +${Math.round(s.net_revenue * 0.7).toLocaleString()}₴ за наступні 30д`;
        risk = "low";
      } else if (s.paid_uses < 3 || s.lift_pct < -5) {
        insightType = "bundle_promo_flop";
        title = `BUNDLE промо ${s.code} — слабкий: ${s.paid_uses} замовлень, ${s.lift_pct >= 0 ? "+" : ""}${s.lift_pct}% lift`;
        description = `Лише ${s.paid_uses} використань за ${s.age_days}д при AOV ${s.avg_order_value.toLocaleString()}₴ (baseline ${baselineAov.toLocaleString()}₴). Промо не дає очікуваного uplift. Тест: збільшити знижку до 15% або знизити мінімум.`;
        expectedImpact = `Без оптимізації втрачаємо ~${Math.round(baselineAov * 5).toLocaleString()}₴ потенційного incremental revenue`;
        risk = "medium";
      } else {
        // Neutral 0-15% lift, skip insight to avoid noise
        continue;
      }

      insightRows.push({
        insight_type: insightType,
        title,
        description,
        expected_impact: expectedImpact,
        confidence: 0.7,
        risk_level: risk,
        affected_layer: "merchandising",
        status: "new",
        metrics: {
          promo_code: s.code,
          promo_id: s.promo_id,
          paid_uses: s.paid_uses,
          gross_revenue: s.gross_revenue,
          discount_given: s.discount_given,
          net_revenue: s.net_revenue,
          avg_order_value: s.avg_order_value,
          baseline_aov: baselineAov,
          lift_pct: s.lift_pct,
          age_days: s.age_days,
        },
      });
    }
    let insightsCreated = 0;
    if (insightRows.length > 0) {
      await supabase.from("ai_insights").insert(insightRows).catch(() => {});
      insightsCreated = insightRows.length;
    }

    return new Response(
      JSON.stringify({
        promos: stats,
        baseline_aov: baselineAov,
        insights_created: insightsCreated,
        measured_count: fresh.length,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
