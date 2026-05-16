// ACOS Price Impact Analyzer
// Why: we apply price_elasticity recommendations blindly — there's no
// feedback whether the change actually improved ATC rate. This function
// closes the loop: for every `price_elasticity_applied` event that's
// 14+ days old, compare the SKU's ATC rate in the 14 days BEFORE the
// change vs the 14 days AFTER. Writes a verdict insight + flips a
// `outcome` field on the original audit row's metadata via a fresh
// `price_elasticity_outcome` event (we don't UPDATE events because the
// table forbids it via RLS — append-only is cleaner anyway).
//
// Verdicts:
//   - win:   ATC rate up ≥10% (relative)
//   - loss:  ATC rate down ≥10%
//   - flat:  within ±10%
//
// Schedule: weekly (Wednesday 10:00 UTC) — analyses any unprocessed
// applied events from 14+ days ago. Idempotent via outcome lookup.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const WINDOW_DAYS = 14;
const WIN_THRESHOLD = 0.10; // ±10% relative change
const MIN_VIEWS_PER_WINDOW = 10; // need signal both sides

interface AppliedMeta {
  previous_price?: number;
  new_price?: number;
  delta_pct?: number;
  insight_id?: string;
  reason?: string;
}

interface Verdict {
  product_id: string;
  product_name: string;
  previous_price: number;
  new_price: number;
  delta_pct: number;
  before_views: number;
  before_atc: number;
  before_rate: number;
  after_views: number;
  after_atc: number;
  after_rate: number;
  rate_change_pct: number;
  verdict: "win" | "loss" | "flat" | "low_signal";
}

async function countEvents(productId: string, type: string, fromIso: string, toIso: string): Promise<number> {
  const { count } = await supabase
    .from("events")
    .select("id", { count: "exact", head: true })
    .eq("product_id", productId)
    .eq("event_type", type)
    .gte("created_at", fromIso)
    .lt("created_at", toIso);
  return count ?? 0;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  try {
    const cutoffIso = new Date(Date.now() - WINDOW_DAYS * 86400_000).toISOString();

    // 1. Get all applied events that are at least WINDOW_DAYS old (so we have full after-window).
    const { data: applied, error: aErr } = await supabase
      .from("events")
      .select("id, product_id, metadata, created_at")
      .eq("event_type", "price_elasticity_applied")
      .lt("created_at", cutoffIso)
      .order("created_at", { ascending: false })
      .limit(50);
    if (aErr) throw aErr;
    if (!applied || applied.length === 0) {
      return new Response(JSON.stringify({ ok: true, analyzed: 0, reason: "no eligible applied events" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // 2. Get already-analyzed product/applied pairs (idempotency).
    const { data: outcomes } = await supabase
      .from("events")
      .select("metadata")
      .eq("event_type", "price_elasticity_outcome")
      .limit(500);
    const analyzedAppliedIds = new Set(
      (outcomes ?? [])
        .map((o) => (o.metadata as { applied_event_id?: string })?.applied_event_id)
        .filter(Boolean) as string[]
    );

    const todo = applied.filter((e) => !analyzedAppliedIds.has(e.id));
    if (todo.length === 0) {
      return new Response(JSON.stringify({ ok: true, analyzed: 0, reason: "all already analyzed" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const outcomeEventRows: any[] = [];

    // Process each todo item in parallel — each already fans out its own Promise.all internally
    const verdicts: Verdict[] = await Promise.all(
      todo.filter((ev) => !!ev.product_id).map(async (ev) => {
        const meta = (ev.metadata ?? {}) as AppliedMeta;
        const appliedAt = new Date(ev.created_at);
        const beforeStart = new Date(appliedAt.getTime() - WINDOW_DAYS * 86400_000).toISOString();
        const afterEnd = new Date(appliedAt.getTime() + WINDOW_DAYS * 86400_000).toISOString();

        const [beforeViews, beforeAtc, afterViews, afterAtc, prodRow] = await Promise.all([
          countEvents(ev.product_id!, "product_viewed", beforeStart, ev.created_at),
          countEvents(ev.product_id!, "add_to_cart", beforeStart, ev.created_at),
          countEvents(ev.product_id!, "product_viewed", ev.created_at, afterEnd),
          countEvents(ev.product_id!, "add_to_cart", ev.created_at, afterEnd),
          supabase.from("products").select("name").eq("id", ev.product_id!).single(),
        ]);

        const beforeRate = beforeViews > 0 ? beforeAtc / beforeViews : 0;
        const afterRate = afterViews > 0 ? afterAtc / afterViews : 0;
        const rateChangePct = beforeRate > 0 ? ((afterRate - beforeRate) / beforeRate) * 100 : 0;

        let verdict: Verdict["verdict"];
        if (beforeViews < MIN_VIEWS_PER_WINDOW || afterViews < MIN_VIEWS_PER_WINDOW) {
          verdict = "low_signal";
        } else if (rateChangePct >= WIN_THRESHOLD * 100) {
          verdict = "win";
        } else if (rateChangePct <= -WIN_THRESHOLD * 100) {
          verdict = "loss";
        } else {
          verdict = "flat";
        }

        const v: Verdict = {
          product_id: ev.product_id!,
          product_name: prodRow.data?.name ?? "Unknown",
          previous_price: meta.previous_price ?? 0,
          new_price: meta.new_price ?? 0,
          delta_pct: meta.delta_pct ?? 0,
          before_views: beforeViews,
          before_atc: beforeAtc,
          before_rate: beforeRate,
          after_views: afterViews,
          after_atc: afterAtc,
          after_rate: afterRate,
          rate_change_pct: rateChangePct,
          verdict,
        };

        // Collect outcome event row (batch insert after loop)
        outcomeEventRows.push({
          event_type: "price_elasticity_outcome",
          product_id: ev.product_id,
          source: "acos",
          metadata: { applied_event_id: ev.id, ...v },
        });

        return v;
      }),
    );

    // Batch insert all outcome events at once
    if (outcomeEventRows.length > 0) {
      await supabase.from("events").insert(outcomeEventRows);
    }

    // 3. Aggregate insight summary
    const wins = verdicts.filter((v) => v.verdict === "win").length;
    const losses = verdicts.filter((v) => v.verdict === "loss").length;
    const flat = verdicts.filter((v) => v.verdict === "flat").length;
    const lowSig = verdicts.filter((v) => v.verdict === "low_signal").length;

    if (verdicts.length > 0) {
      const lines = verdicts
        .filter((v) => v.verdict !== "low_signal")
        .slice(0, 10)
        .map(
          (v) =>
            `${v.verdict === "win" ? "✅" : v.verdict === "loss" ? "❌" : "➖"} ${v.product_name}: ${v.previous_price}→${v.new_price}₴ · ATC rate ${(v.before_rate * 100).toFixed(1)}%→${(v.after_rate * 100).toFixed(1)}% (${v.rate_change_pct >= 0 ? "+" : ""}${v.rate_change_pct.toFixed(1)}%)`
        );

      await supabase.from("ai_insights").insert({
        insight_type: "price_impact_review",
        title: `Цінові зміни: ${wins} виграли · ${losses} програли · ${flat} без змін`,
        description: lines.length > 0 ? lines.join("\n") : `Проаналізовано ${verdicts.length} застосованих змін за останні ${WINDOW_DAYS}+ днів. Усі мають недостатній сигнал (< ${MIN_VIEWS_PER_WINDOW} переглядів за вікно).`,
        expected_impact:
          wins > losses
            ? `Heuristic працює: win-rate ${Math.round((wins / Math.max(1, wins + losses + flat)) * 100)}%. Можна збільшити confidence у acos-price-elasticity.`
            : losses > wins
            ? `Heuristic слабка: ${losses} loss vs ${wins} win. Розглянь rollback losses або зниження ELASTICITY_EXPONENT.`
            : "Нейтрально — потрібна більша вибірка.",
        confidence: 0.8,
        risk_level: losses > wins ? "high" : "low",
        affected_layer: "pricing",
        metrics: {
          wins,
          losses,
          flat,
          low_signal: lowSig,
          window_days: WINDOW_DAYS,
          win_threshold_pct: WIN_THRESHOLD * 100,
          verdicts,
        },
      });
    }

    return new Response(
      JSON.stringify({ ok: true, analyzed: verdicts.length, wins, losses, flat, low_signal: lowSig }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error("[acos-price-impact-analyzer]", err);
    return new Response(JSON.stringify({ ok: false, error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
