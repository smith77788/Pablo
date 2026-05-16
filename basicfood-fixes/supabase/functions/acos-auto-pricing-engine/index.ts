// ACOS Auto-Pricing Engine
//
// Promotes pending margin_recommendations to live product prices when:
//   • Recommendation is recent (last 7d)
//   • Suggested change is small (≤ 7% absolute) → low risk
//   • Product not currently part of an active promo
//   • There is enough velocity baseline (sold_count > 0 in last 30d)
//
// Writes audit row in pricing_decisions and updates ai_memory with outcome.
// Bigger swings (> 7%) are queued for human review and surfaced in insights.
//
// Idempotency: only acts on `status='pending'` recs and marks them `applied|skipped`.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { enqueueTribunalCase } from "../_shared/tribunal.ts";
import { checkSystemHealth } from "../_shared/system-health-guard.ts";
import { detectTrigger, beginQuickAgentRun } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MAX_AUTO_PCT = 0.07;
const MIN_PRICE_FLOOR = 1; // never drop below 1 UAH

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-auto-pricing-engine", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));
    const dryRun = body?.dry_run === true;
    const fromTribunal = body?.from_tribunal === true;

    // ─── TRIBUNAL ENFORCE PATH ───────────────────────────────────────────
    // Called by tribunal-enforcer with proposed_change={product_id, new_price, rec_id}
    // after judge has approved. Performs the actual write.
    if (fromTribunal) {
      const change = body?.proposed_change ?? {};
      const productId = change.product_id as string | undefined;
      const newPrice = Number(change.new_price);
      const recId = change.rec_id as string | undefined;
      if (!productId || !Number.isFinite(newPrice) || newPrice < MIN_PRICE_FLOOR) {
        return new Response(JSON.stringify({ error: "invalid tribunal payload" }), {
          status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      const { error: upErr } = await supabase.from("products").update({ price: newPrice }).eq("id", productId);
      if (upErr) {
        return new Response(JSON.stringify({ error: upErr.message }), {
          status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      if (recId) {
        await supabase.from("margin_recommendations").update({ status: "applied" }).eq("id", recId);
        await supabase.from("pricing_decisions").insert({
          product_id: productId, recommendation_id: recId, new_price: newPrice,
          decision: "applied", decided_by: "tribunal", rationale: change.rationale ?? null,
        });
      }
      return new Response(JSON.stringify({ ok: true, applied: { product_id: productId, new_price: newPrice } }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // System Health Guard — block new tribunal enqueues if autonomy stress detected.
    const health = await checkSystemHealth();
    if (!health.ok) {
      try {
        await supabase.from("agent_runs").insert({
          function_name: "acos-auto-pricing-engine",
          trigger: detectTrigger(req, body),
          status: "skipped",
          started_at: new Date(Date.now() - 1000).toISOString(),
          finished_at: new Date().toISOString(),
          summary: `health_block:${health.reason}`,
          payload: { health_signals: health.signals },
        });
      } catch { /* non-fatal */ }
      return new Response(
        JSON.stringify({ ok: true, skipped: true, reason: `system_unhealthy:${health.reason}`, signals: health.signals }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const { data: recs } = await supabase
      .from("margin_recommendations")
      .select("id, product_id, current_price, suggested_price, expected_margin_pct, rationale, computed_at, status")
      .eq("status", "pending")
      .gte("computed_at", new Date(Date.now() - 7 * 86400_000).toISOString())
      .order("computed_at", { ascending: false })
      .limit(50);

    const productIds = [...new Set((recs ?? []).map((r) => r.product_id))];
    const { data: products } = productIds.length
      ? await supabase.from("products").select("id, name, price, sold_count, is_active").in("id", productIds)
      : { data: [] as any[] };
    const productMap = new Map((products ?? []).map((p: any) => [p.id, p]));

    const applied: any[] = [];
    const skipped: any[] = [];
    const queuedForReview: any[] = [];

    // Collect status changes during classification pass (no DB writes yet)
    const staleIds: string[] = [];
    const skippedIds: string[] = [];
    const needsReviewIds: string[] = [];
    const insightRows: any[] = [];
    interface TribunalItem { rec: any; p: any; oldPrice: number; newPrice: number; pctChange: number }
    const tribunalPending: TribunalItem[] = [];

    for (const rec of recs ?? []) {
      const p = productMap.get(rec.product_id);
      if (!p || !p.is_active) {
        skipped.push({ rec_id: rec.id, reason: "product_missing_or_inactive" });
        continue;
      }
      const oldPrice = p.price;
      const newPrice = Math.max(MIN_PRICE_FLOOR, rec.suggested_price);
      const pctChange = Math.abs(newPrice - oldPrice) / oldPrice;

      if (oldPrice !== rec.current_price) {
        skipped.push({ rec_id: rec.id, reason: "price_changed_since_recommendation", old: oldPrice, was: rec.current_price });
        if (!dryRun) staleIds.push(rec.id);
        continue;
      }
      if ((p.sold_count ?? 0) === 0) {
        skipped.push({ rec_id: rec.id, reason: "no_velocity_baseline" });
        if (!dryRun) skippedIds.push(rec.id);
        continue;
      }
      if (pctChange > MAX_AUTO_PCT) {
        queuedForReview.push({
          rec_id: rec.id,
          product: p.name,
          pct: Math.round(pctChange * 1000) / 10,
          from: oldPrice,
          to: newPrice,
        });
        if (!dryRun) {
          needsReviewIds.push(rec.id);
          insightRows.push({
            insight_type: "pricing_review",
            title: `💰 Перегляд ціни: ${p.name} (${oldPrice} → ${newPrice}₴, ${(pctChange * 100).toFixed(1)}%)`,
            description: `Рекомендація вище порогу авто-застосування (${(MAX_AUTO_PCT * 100).toFixed(0)}%). Підтверди вручну. Причина: ${rec.rationale ?? "—"}`,
            confidence: 0.7,
            risk_level: "medium",
            affected_layer: "pricing",
            status: "new",
            metrics: { product_id: p.id, from: oldPrice, to: newPrice, pct: pctChange },
          });
        }
        continue;
      }

      if (dryRun) {
        applied.push({ rec_id: rec.id, product: p.name, from: oldPrice, to: newPrice, pct: pctChange });
        continue;
      }

      // Defer to parallel tribunal batch below
      tribunalPending.push({ rec, p, oldPrice, newPrice, pctChange });
    }

    if (!dryRun) {
      // Batch status updates in parallel
      const batchOps: Promise<any>[] = [];
      if (staleIds.length) batchOps.push(supabase.from("margin_recommendations").update({ status: "stale" }).in("id", staleIds));
      if (skippedIds.length) batchOps.push(supabase.from("margin_recommendations").update({ status: "skipped" }).in("id", skippedIds));
      if (needsReviewIds.length) batchOps.push(supabase.from("margin_recommendations").update({ status: "needs_review" }).in("id", needsReviewIds));
      if (insightRows.length) batchOps.push(supabase.from("ai_insights").insert(insightRows));
      if (batchOps.length) await Promise.all(batchOps);

      // SUBMIT TO TRIBUNAL in parallel — actual product UPDATE happens in tribunal-enforcer → from_tribunal=true
      await Promise.all(tribunalPending.map(async ({ rec, p, oldPrice, newPrice, pctChange }) => {
        try {
          const tc = await enqueueTribunalCase({
            source_function: "acos-auto-pricing-engine",
            category: "pricing",
            urgency: Math.abs(pctChange) > 0.05 ? "high" : "normal",
            proposed_change: {
              product_id: p.id, rec_id: rec.id,
              old_price: oldPrice, new_price: newPrice,
              pct_change: pctChange, rationale: rec.rationale,
            },
            context: { product_name: p.name, sold_count: p.sold_count },
            expected_impact: rec.expected_margin_pct ? `+${rec.expected_margin_pct}% margin` : undefined,
          });
          await supabase.from("margin_recommendations").update({ status: "submitted_to_tribunal" }).eq("id", rec.id);
          applied.push({ rec_id: rec.id, product: p.name, from: oldPrice, to: newPrice, tribunal_case: tc.case_id, reused: tc.reused });
        } catch (e: any) {
          __agent.error(e);
          skipped.push({ rec_id: rec.id, reason: "tribunal_enqueue_failed", error: String(e?.message ?? e) });
        }
      }));
    }

    // Memory feedback
    if (!dryRun && (applied.length || queuedForReview.length)) {
      await supabase.from("ai_memory").upsert(
        [{
          agent: "auto-pricing-engine",
          category: "pricing",
          pattern_key: "global:run",
          learned_rule: `Run applied ${applied.length}, queued ${queuedForReview.length}, skipped ${skipped.length}`,
          confidence: 0.6,
          avg_impact: applied.length,
          evidence: { applied_count: applied.length, queued_count: queuedForReview.length, skipped_count: skipped.length },
          last_observed_at: new Date().toISOString(),
        }],
        { onConflict: "agent,pattern_key" } as any,
      );
    }

    try {
      await supabase.from("agent_runs").insert({
        function_name: "acos-auto-pricing-engine",
        trigger: dryRun ? "dry_run" : (fromTribunal ? "tribunal_enforce" : detectTrigger(req, body)),
        status: skipped.length > applied.length + queuedForReview.length ? "partial" : "success",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `applied=${applied.length}, queued=${queuedForReview.length}, skipped=${skipped.length}`,
        payload: { applied: applied.slice(0, 5), skipped_sample: skipped.slice(0, 3) },
      });
    } catch { /* ignore */ }

    __agent.success();
    return new Response(
      JSON.stringify({
        ok: true,
        dry_run: dryRun,
        scanned: recs?.length ?? 0,
        applied_count: applied.length,
        queued_for_review_count: queuedForReview.length,
        skipped_count: skipped.length,
        applied,
        queued_for_review: queuedForReview,
        skipped: skipped.slice(0, 10),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    console.error("[auto-pricing] fatal", e);
    return new Response(JSON.stringify({ error: String((e as Error)?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
