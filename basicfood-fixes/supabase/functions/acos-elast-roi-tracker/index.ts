// ACOS ELAST ROI Tracker — measures real performance of ELAST{SKU}{PCT} promos
// created by acos-elasticity-auto-apply. For each ELAST promo aged 7-45 days,
// it pulls actual usage, net revenue, and computes lift vs predicted, then
// updates the source `elasticity_auto_applied` insight with status winner/flop
// and emits a roll-up `elast_roi_summary` insight per run.
//
// Logic:
//   - winner: real_lift_pct >= 50% of predicted lift AND uses >= 3
//   - flop:   uses == 0 (after 14d) OR real_lift_pct < 0
//   - inconclusive: in between, awaits more data
//
// Mirrors acos-bundle-promo-roi pattern but scoped to single-SKU elasticity tests.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const MIN_AGE_DAYS = 7;       // need at least 7d of data before judging
const MAX_AGE_DAYS = 45;      // ignore promos older than 45d (already closed)
const FLOP_AGE_DAYS = 14;     // 0 uses after 14d → definitive flop
const BASELINE_WINDOW_DAYS = 60;
const MIN_USES_WINNER = 3;
const WINNER_LIFT_THRESHOLD_RATIO = 0.5; // realized ≥ 50% of predicted = winner

interface ElastInsight {
  id: string;
  created_at: string;
  status: string;
  metrics: {
    promo_code?: string;
    promo_id?: string;
    product_id?: string;
    product_name?: string;
    discount_pct?: number;
    expected_lift_pct?: number;
    source_insight_id?: string;
  } | null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-elast-roi-tracker", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const now = Date.now();
    const minCreated = new Date(now - MAX_AGE_DAYS * dayMs).toISOString();
    const maxCreated = new Date(now - MIN_AGE_DAYS * dayMs).toISOString();

    // 1. Pull ELAST tracking insights in evaluation window, still "new".
    const { data: tracking } = await supabase
      .from("ai_insights")
      .select("id, created_at, status, metrics")
      .eq("insight_type", "elasticity_auto_applied")
      .eq("status", "new")
      .gte("created_at", minCreated)
      .lte("created_at", maxCreated)
      .limit(50);

    const candidates = ((tracking ?? []) as ElastInsight[]).filter((t) => {
      const m = t.metrics ?? {};
      return !!m.promo_code && !!m.product_id && typeof m.discount_pct === "number";
    });

    if (candidates.length === 0) {
      return new Response(
        JSON.stringify({ evaluated: 0, reason: "no_candidates_in_window" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull baseline AOV across all orders in last 60d for lift comparison.
    const baselineCutoff = new Date(now - BASELINE_WINDOW_DAYS * dayMs).toISOString();
    const { data: baseOrders } = await supabase
      .from("orders")
      .select("total")
      .gte("created_at", baselineCutoff)
      .neq("status", "cancelled")
      .limit(2000);
    const baselineAov =
      (baseOrders ?? []).length > 0
        ? Math.round(
            (baseOrders ?? []).reduce((s, o) => s + (o.total ?? 0), 0) /
              (baseOrders ?? []).length,
          )
        : 0;

    // 3. Pull all promo_codes for these ELAST codes to get current_uses.
    const codes = candidates.map((c) => c.metrics!.promo_code!);
    const { data: promoRows } = await supabase
      .from("promo_codes")
      .select("id, code, current_uses, is_active, created_at")
      .in("code", codes);
    const promoByCode = new Map(
      (promoRows ?? []).map((p) => [p.code.toUpperCase(), p]),
    );

    // 4. Pre-fetch all promo uses and orders in batch to avoid N+1.
    const activePromoIds = [...promoByCode.values()]
      .filter((p) => (p.current_uses ?? 0) > 0)
      .map((p) => p.id);
    const usesMap = new Map<string, string[]>(); // promo_id → order_ids
    if (activePromoIds.length > 0) {
      const { data: allUsesRows } = await supabase
        .from("promo_code_uses")
        .select("promo_code_id, order_id")
        .in("promo_code_id", activePromoIds)
        .limit(2000);
      for (const u of allUsesRows ?? []) {
        if (!u.order_id) continue;
        const arr = usesMap.get(u.promo_code_id) ?? [];
        arr.push(u.order_id);
        usesMap.set(u.promo_code_id, arr);
      }
    }
    const allOrderIds = [...new Set([...usesMap.values()].flat())];
    const orderById = new Map<string, { total: number; status: string }>();
    if (allOrderIds.length > 0) {
      const { data: allOrdRows } = await supabase
        .from("orders")
        .select("id, total, status")
        .in("id", allOrderIds);
      for (const o of allOrdRows ?? []) orderById.set(o.id, o);
    }

    const results: Array<{
      insight_id: string;
      promo_code: string;
      product_name: string;
      uses: number;
      net_revenue: number;
      avg_order_value: number;
      predicted_lift_pct: number;
      realized_lift_pct: number;
      verdict: "winner" | "flop" | "inconclusive";
      age_days: number;
    }> = [];

    for (const ins of candidates) {
      const m = ins.metrics!;
      const code = m.promo_code!.toUpperCase();
      const promoRow = promoByCode.get(code);
      const ageDays = Math.floor((now - new Date(ins.created_at).getTime()) / dayMs);

      let uses = promoRow?.current_uses ?? 0;
      let netRevenue = 0;
      let aov = 0;

      if (promoRow && uses > 0) {
        const orderIds = usesMap.get(promoRow.id) ?? [];
        if (orderIds.length > 0) {
          const valid = orderIds.map((id) => orderById.get(id)).filter((o) => o && o.status !== "cancelled") as { total: number; status: string }[];
          netRevenue = valid.reduce((s, o) => s + (o.total ?? 0), 0);
          aov = valid.length > 0 ? Math.round(netRevenue / valid.length) : 0;
          uses = valid.length;
        }
      }

      const predictedLift = m.expected_lift_pct ?? 0;
      const realizedLift =
        baselineAov > 0 && aov > 0
          ? Math.round(((aov - baselineAov) / baselineAov) * 100)
          : 0;

      let verdict: "winner" | "flop" | "inconclusive";
      if (uses === 0 && ageDays >= FLOP_AGE_DAYS) {
        verdict = "flop";
      } else if (
        uses >= MIN_USES_WINNER &&
        realizedLift >= predictedLift * WINNER_LIFT_THRESHOLD_RATIO
      ) {
        verdict = "winner";
      } else if (uses >= MIN_USES_WINNER && realizedLift < 0) {
        verdict = "flop";
      } else {
        verdict = "inconclusive";
      }

      results.push({
        insight_id: ins.id,
        promo_code: code,
        product_name: m.product_name ?? "—",
        uses,
        net_revenue: netRevenue,
        avg_order_value: aov,
        predicted_lift_pct: predictedLift,
        realized_lift_pct: realizedLift,
        verdict,
        age_days: ageDays,
      });

      // Update tracking insight status when we have a verdict (winner/flop).
      if (verdict !== "inconclusive") {
        await supabase
          .from("ai_insights")
          .update({
            status: verdict === "winner" ? "validated" : "rejected",
            metrics: {
              ...m,
              roi_evaluated_at: new Date(now).toISOString(),
              roi_uses: uses,
              roi_net_revenue: netRevenue,
              roi_avg_order_value: aov,
              roi_realized_lift_pct: realizedLift,
              roi_verdict: verdict,
            },
          })
          .eq("id", ins.id);

        // Auto-deactivate flop promos to stop wasting impressions
        if (verdict === "flop" && promoRow && promoRow.is_active) {
          await supabase
            .from("promo_codes")
            .update({ is_active: false })
            .eq("id", promoRow.id);
        }
      }
    }

    // 5. Emit roll-up summary insight only if there are decided verdicts.
    const winners = results.filter((r) => r.verdict === "winner");
    const flops = results.filter((r) => r.verdict === "flop");
    const decided = winners.length + flops.length;

    if (decided > 0) {
      const totalNet = winners.reduce((s, w) => s + w.net_revenue, 0);
      const winRate = Math.round((winners.length / decided) * 100);
      await supabase.from("ai_insights").insert({
        insight_type: "elast_roi_summary",
        title: `🎯 ELAST ROI: ${winners.length}W / ${flops.length}F (${winRate}% win-rate)`,
        description: `Оцінено ${decided} ELAST A/B-тест(ів): ${winners.length} winner(s) принесли ${totalNet.toLocaleString()}₴ net revenue, ${flops.length} flop(s) деактивовано автоматично. Top winner: ${
          winners[0]
            ? `${winners[0].promo_code} (${winners[0].product_name}) +${winners[0].realized_lift_pct}% AOV`
            : "—"
        }. Top flop: ${flops[0] ? `${flops[0].promo_code} (${flops[0].product_name})` : "—"}.`,
        expected_impact: `${totalNet.toLocaleString()}₴ net revenue зафіксовано від winner-тестів за оціночний період`,
        confidence: 0.85,
        risk_level: "low",
        affected_layer: "merchandising",
        status: "new",
        metrics: {
          evaluated: results.length,
          winners: winners.length,
          flops: flops.length,
          inconclusive: results.length - decided,
          win_rate_pct: winRate,
          total_winner_net_revenue: totalNet,
          baseline_aov: baselineAov,
          top_winners: winners.slice(0, 3),
          top_flops: flops.slice(0, 3),
        },
      });
    }

    __agent.success();
    return new Response(
      JSON.stringify({
        evaluated: results.length,
        winners: winners.length,
        flops: flops.length,
        inconclusive: results.length - decided,
        baseline_aov: baselineAov,
        results,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    __agent.error(err);
        return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
