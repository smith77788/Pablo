// ACOS Iter-7 — A/B test evaluator (cron 6h).
// For each running seo_experiment with ≥100 impressions on EACH variant,
// computes click-through and conversion rate, picks winner, and (if winner is B)
// auto-applies it to seo_overrides. Logs ai_insight for transparency.
// SAFE: never touches checkout/payment/auth. Only mutates seo_experiments + seo_overrides.

import "https://deno.land/x/xhr@0.1.0/mod.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MIN_IMPRESSIONS = 100;
// Minimum lift on the winning variant vs loser to declare a winner (conv-rate).
// Below this threshold → "inconclusive" (test continues or admin decides).
const MIN_LIFT = 0.10;

interface Experiment {
  id: string;
  page_path: string;
  variant_a_impressions: number;
  variant_b_impressions: number;
  variant_a_clicks: number;
  variant_b_clicks: number;
  variant_a_purchases: number;
  variant_b_purchases: number;
  variant_a_h1: string | null;
  variant_a_meta_title: string | null;
  variant_a_meta_description: string | null;
  variant_a_keywords: string[];
  variant_b_h1: string | null;
  variant_b_meta_title: string | null;
  variant_b_meta_description: string | null;
  variant_b_keywords: string[];
  source_insight_id: string | null;
}

const rate = (numerator: number, denominator: number) =>
  denominator > 0 ? numerator / denominator : 0;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const { data: experiments, error } = await supabase
    .from("seo_experiments")
    .select(
      "id, page_path, variant_a_impressions, variant_b_impressions, variant_a_clicks, variant_b_clicks, variant_a_purchases, variant_b_purchases, variant_a_h1, variant_a_meta_title, variant_a_meta_description, variant_a_keywords, variant_b_h1, variant_b_meta_title, variant_b_meta_description, variant_b_keywords, source_insight_id",
    )
    .eq("status", "running");

  if (error) {
    return new Response(JSON.stringify({ ok: false, error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const results: Array<Record<string, unknown>> = await Promise.all(
    ((experiments ?? []) as Experiment[]).map(async (exp): Promise<Record<string, unknown>> => {
      const aImp = exp.variant_a_impressions;
      const bImp = exp.variant_b_impressions;

      if (aImp < MIN_IMPRESSIONS || bImp < MIN_IMPRESSIONS) {
        return { experiment_id: exp.id, action: "skip", reason: "insufficient_data", a_imp: aImp, b_imp: bImp };
      }

      const aConv = rate(exp.variant_a_purchases, aImp);
      const bConv = rate(exp.variant_b_purchases, bImp);

      let winner: "a" | "b" | "tie" = "tie";
      let newStatus: "winner_a" | "winner_b" | "inconclusive" = "inconclusive";

      if (aConv === 0 && bConv === 0) {
        // Fallback to clicks if zero purchases on both
        const aCtr = rate(exp.variant_a_clicks, aImp);
        const bCtr = rate(exp.variant_b_clicks, bImp);
        const liftCtr = aCtr > 0 ? Math.abs(bCtr - aCtr) / aCtr : (bCtr > 0 ? 1 : 0);
        if (liftCtr >= MIN_LIFT) {
          winner = bCtr > aCtr ? "b" : "a";
          newStatus = winner === "a" ? "winner_a" : "winner_b";
        }
      } else {
        const lift = aConv > 0 ? Math.abs(bConv - aConv) / aConv : (bConv > 0 ? 1 : 0);
        if (lift >= MIN_LIFT) {
          winner = bConv > aConv ? "b" : "a";
          newStatus = winner === "a" ? "winner_a" : "winner_b";
        }
      }

      const updatePayload: Record<string, unknown> = {
        status: newStatus,
        winner,
        decided_at: new Date().toISOString(),
      };

      // Auto-apply only if B wins — A is current state, no override needed.
      let overrideApplied = false;
      if (winner === "b") {
        const { error: upErr } = await supabase.from("seo_overrides").upsert(
          {
            page_path: exp.page_path,
            h1: exp.variant_b_h1,
            meta_title: exp.variant_b_meta_title,
            meta_description: exp.variant_b_meta_description,
            keywords: exp.variant_b_keywords,
            source: "ab_test_winner",
            applied_from_insight_id: exp.source_insight_id,
          },
          { onConflict: "page_path" },
        );
        if (upErr) {
          return { experiment_id: exp.id, action: "apply_failed", error: upErr.message };
        }
        overrideApplied = true;
        updatePayload.applied_to_overrides_at = new Date().toISOString();
      }

      await Promise.all([
        supabase.from("seo_experiments").update(updatePayload).eq("id", exp.id),
        supabase.from("ai_insights").insert({
          insight_type: "seo_ab_test_result",
          title: `A/B test ${newStatus} on ${exp.page_path}`,
          description: `Variant A conv: ${(rate(exp.variant_a_purchases, aImp) * 100).toFixed(2)}% (${aImp} imp) · Variant B conv: ${(rate(exp.variant_b_purchases, bImp) * 100).toFixed(2)}% (${bImp} imp). Winner: ${winner}.${overrideApplied ? " Auto-applied to overrides." : ""}`,
          affected_layer: "seo",
          risk_level: "low",
          confidence: 0.9,
          metrics: {
            page_path: exp.page_path,
            variant_a: { impressions: aImp, clicks: exp.variant_a_clicks, purchases: exp.variant_a_purchases },
            variant_b: { impressions: bImp, clicks: exp.variant_b_clicks, purchases: exp.variant_b_purchases },
            winner,
            applied: overrideApplied,
          },
          status: "new",
        }),
      ]);

      return {
        experiment_id: exp.id,
        action: "decided",
        winner,
        status: newStatus,
        applied: overrideApplied,
      };
    }),
  );

  return new Response(
    JSON.stringify({ ok: true, evaluated: results.length, results }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
