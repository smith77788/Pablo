// ACOS Iter-13 — Bot Welcome A/B Auto-Evaluator
// Cron: every 12h. Scans `running` bot_experiments and auto-decides a winner
// when statistical signal is strong enough. Saves an ai_insight + marks winner.
//
// Decision rules (Stability-agent guardrails — favour false negatives over
// false positives so we don't ship a worse welcome based on noise):
//   - Each variant must have >= MIN_IMPRESSIONS (default 100)
//   - CTR delta must be >= MIN_CTR_DELTA_PCT (default 15%)
//   - Winner CTR must be > 0
// If both metrics meet the bar, we pick the higher-CTR variant. Purchases are
// a tie-breaker only (low volume bot funnel — purchases are too sparse early).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MIN_IMPRESSIONS = 100;
const MIN_CTR_DELTA_PCT = 15;

interface BotExp {
  id: string;
  name: string;
  status: string;
  variant_a_text: string;
  variant_b_text: string;
  variant_a_impressions: number;
  variant_a_button_clicks: number;
  variant_a_purchases: number;
  variant_b_impressions: number;
  variant_b_button_clicks: number;
  variant_b_purchases: number;
}

function ctr(clicks: number, impressions: number) {
  return impressions > 0 ? (clicks / impressions) * 100 : 0;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-bot-welcome-evaluator", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const { data: experiments, error } = await supabase
    .from("bot_experiments")
    .select("*")
    .eq("status", "running");

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Process experiments in parallel; within each, update + insight insert run concurrently
  const decisions: Array<Record<string, unknown>> = (await Promise.all(
    ((experiments as BotExp[]) ?? []).map(async (exp): Promise<Record<string, unknown> | null> => {
      const aImp = exp.variant_a_impressions;
      const bImp = exp.variant_b_impressions;

      if (aImp < MIN_IMPRESSIONS || bImp < MIN_IMPRESSIONS) return null;

      const aCtr = ctr(exp.variant_a_button_clicks, aImp);
      const bCtr = ctr(exp.variant_b_button_clicks, bImp);

      if (aCtr === 0 && bCtr === 0) return null;

      // relative delta — robust when one CTR is tiny
      const baseline = Math.max(aCtr, bCtr, 0.01);
      const deltaPct = (Math.abs(aCtr - bCtr) / baseline) * 100;

      if (deltaPct < MIN_CTR_DELTA_PCT) return null;

      let winner: "a" | "b" = aCtr > bCtr ? "a" : "b";
      // tie-breaker: if CTRs are within 2pp, prefer purchases
      if (Math.abs(aCtr - bCtr) < 2) {
        winner = exp.variant_a_purchases >= exp.variant_b_purchases ? "a" : "b";
      }

      const winnerText = winner === "a" ? exp.variant_a_text : exp.variant_b_text;
      const loserText = winner === "a" ? exp.variant_b_text : exp.variant_a_text;
      const winnerCtr = winner === "a" ? aCtr : bCtr;
      const loserCtr = winner === "a" ? bCtr : aCtr;

      // 1) mark experiment as decided + 2) record insight — in parallel
      await Promise.all([
        supabase.from("bot_experiments").update({
          status: "decided",
          winner,
          decided_at: new Date().toISOString(),
        }).eq("id", exp.id),
        supabase.from("ai_insights").insert({
          insight_type: "bot_welcome_winner",
          title: `Bot welcome A/B: переможець варіант ${winner.toUpperCase()}`,
          description: `«${exp.name}» — варіант ${winner.toUpperCase()} переміг з CTR ${winnerCtr.toFixed(1)}% проти ${loserCtr.toFixed(1)}% (Δ ${deltaPct.toFixed(1)}%). Текст переможця: "${winnerText.slice(0, 120)}…"`,
          confidence: Math.min(0.95, 0.5 + deltaPct / 100),
          risk_level: "low",
          affected_layer: "bot",
          expected_impact: `+${deltaPct.toFixed(0)}% CTR в боті`,
          status: "auto_applied",
          metrics: {
            experiment_id: exp.id,
            variant_a_ctr: aCtr,
            variant_b_ctr: bCtr,
            delta_pct: deltaPct,
            winner_text: winnerText,
            loser_text: loserText,
          },
        }),
      ]);

      return { experiment_id: exp.id, name: exp.name, winner, delta_pct: Number(deltaPct.toFixed(1)) };
    }),
  )).filter(Boolean) as Array<Record<string, unknown>>;

  return new Response(
    JSON.stringify({ ok: true, decided: decisions.length, decisions }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
    })();
    return { response: __res };
  });
});
