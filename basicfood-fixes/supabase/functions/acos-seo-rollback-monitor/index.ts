// ACOS Iter-9 — Stability Engineer: SEO auto-rollback monitor.
// Runs hourly via pg_cron. For each seo_override that came from `ab_test_winner`:
//   1. Compute conversion rate over last 24h (post-apply window).
//   2. Compute baseline conversion rate over the 7 days BEFORE the override was applied.
//   3. If current < baseline * 0.8 (i.e. >20% relative drop) AND we have ≥30 sessions
//      in the post-apply window (statistical floor), DELETE the override and log it.
//   4. Mark the source experiment with rolled_back_at so the evaluator won't re-apply.
// SAFE: only touches seo_overrides + seo_experiments + seo_rollback_log + ai_insights.
// Never touches checkout / payment / auth.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const POST_WINDOW_HOURS = 24;
const BASELINE_WINDOW_DAYS = 7;
const MIN_POST_SESSIONS = 30;
const DROP_THRESHOLD = 0.20; // 20% relative drop

interface OverrideRow {
  id: string;
  page_path: string;
  h1: string | null;
  meta_title: string | null;
  meta_description: string | null;
  keywords: string[];
  source: string;
  applied_from_insight_id: string | null;
  created_at: string;
  updated_at: string;
}

interface ConvStats {
  sessions: number;
  purchases: number;
  rate: number;
}

const computeConvRate = async (
  supabase: any,
  pagePath: string,
  fromIso: string,
  toIso: string,
): Promise<ConvStats> => {
  // Sessions = unique session_id with page_view on this page in window.
  const { data: views } = await supabase
    .from("events")
    .select("session_id")
    .eq("event_type", "page_view")
    .eq("url", pagePath)
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .limit(10000);

  const sessionSet = new Set<string>();
  for (const row of views ?? []) {
    if (row.session_id) sessionSet.add(row.session_id as string);
  }
  const sessions = sessionSet.size;

  // Purchases attributed to those sessions.
  let purchases = 0;
  if (sessions > 0) {
    const sessionIds = Array.from(sessionSet);
    // Parallel chunks to avoid huge IN clauses.
    const chunkSize = 200;
    const countChunks: string[][] = [];
    for (let i = 0; i < sessionIds.length; i += chunkSize) countChunks.push(sessionIds.slice(i, i + chunkSize));
    const countResults = await Promise.all(
      countChunks.map((chunk) =>
        supabase.from("events").select("*", { count: "exact", head: true })
          .eq("event_type", "purchase_completed").in("session_id", chunk)
          .gte("created_at", fromIso).lte("created_at", toIso)
      ),
    );
    for (const { count } of countResults) purchases += count ?? 0;
  }

  return {
    sessions,
    purchases,
    rate: sessions > 0 ? purchases / sessions : 0,
  };
};

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

  const now = new Date();
  const postFrom = new Date(now.getTime() - POST_WINDOW_HOURS * 3600 * 1000);

  // Only check overrides that came from A/B winners and were applied >24h ago
  // (so we have a full post-window of data) and not modified manually after.
  const { data: overrides, error } = await supabase
    .from("seo_overrides")
    .select("id, page_path, h1, meta_title, meta_description, keywords, source, applied_from_insight_id, created_at, updated_at")
    .eq("source", "ab_test_winner")
    .lte("updated_at", postFrom.toISOString());

  if (error) {
    return new Response(JSON.stringify({ ok: false, error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const results: Array<Record<string, unknown>> = [];

  for (const ov of (overrides ?? []) as OverrideRow[]) {
    const appliedAt = new Date(ov.updated_at);
    const baselineTo = appliedAt;
    const baselineFrom = new Date(appliedAt.getTime() - BASELINE_WINDOW_DAYS * 24 * 3600 * 1000);

    const [baseline, current] = await Promise.all([
      computeConvRate(supabase, ov.page_path, baselineFrom.toISOString(), baselineTo.toISOString()),
      computeConvRate(supabase, ov.page_path, postFrom.toISOString(), now.toISOString()),
    ]);

    // Need enough post-window sessions to make a decision.
    if (current.sessions < MIN_POST_SESSIONS) {
      results.push({ page_path: ov.page_path, action: "skip", reason: "insufficient_post_sessions", post_sessions: current.sessions });
      continue;
    }

    // If baseline had no conversions, we can't measure a "drop" — skip safely.
    if (baseline.rate <= 0) {
      results.push({ page_path: ov.page_path, action: "skip", reason: "no_baseline_conversions" });
      continue;
    }

    const dropPct = (baseline.rate - current.rate) / baseline.rate;
    if (dropPct < DROP_THRESHOLD) {
      results.push({
        page_path: ov.page_path,
        action: "keep",
        baseline_rate: baseline.rate,
        current_rate: current.rate,
        drop_pct: dropPct,
      });
      continue;
    }

    // ROLLBACK: snapshot, delete override, mark experiment, log.
    const snapshot = {
      h1: ov.h1,
      meta_title: ov.meta_title,
      meta_description: ov.meta_description,
      keywords: ov.keywords,
      source: ov.source,
      applied_from_insight_id: ov.applied_from_insight_id,
    };

    // Find source experiment (most recent winner_b for this page, not yet rolled back).
    const { data: exp } = await supabase
      .from("seo_experiments")
      .select("id")
      .eq("page_path", ov.page_path)
      .eq("status", "winner_b")
      .is("rolled_back_at", null)
      .order("decided_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    const { error: delErr } = await supabase
      .from("seo_overrides")
      .delete()
      .eq("id", ov.id);

    if (delErr) {
      results.push({ page_path: ov.page_path, action: "rollback_failed", error: delErr.message });
      continue;
    }

    await supabase.from("seo_rollback_log").insert({
      page_path: ov.page_path,
      experiment_id: exp?.id ?? null,
      override_snapshot: snapshot,
      baseline_conv_rate: baseline.rate,
      current_conv_rate: current.rate,
      drop_pct: dropPct,
      reason: "conversion_drop",
    });

    if (exp?.id) {
      await supabase
        .from("seo_experiments")
        .update({ rolled_back_at: new Date().toISOString() })
        .eq("id", exp.id);
    }

    await supabase.from("ai_insights").insert({
      insight_type: "seo_auto_rollback",
      affected_layer: "seo",
      risk_level: "medium",
      title: `Auto-rollback: ${ov.page_path}`,
      description: `Stability agent відкотив SEO winner на ${ov.page_path}. Конверсія впала з ${(baseline.rate * 100).toFixed(2)}% до ${(current.rate * 100).toFixed(2)}% (-${(dropPct * 100).toFixed(1)}%) за останні 24h. Override видалено, сторінка повернулась на baseline.`,
      confidence: 0.85,
      metrics: {
        page_path: ov.page_path,
        baseline_sessions: baseline.sessions,
        baseline_purchases: baseline.purchases,
        baseline_rate: baseline.rate,
        current_sessions: current.sessions,
        current_purchases: current.purchases,
        current_rate: current.rate,
        drop_pct: dropPct,
        snapshot,
      },
      status: "new",
    });

    results.push({
      page_path: ov.page_path,
      action: "rolled_back",
      baseline_rate: baseline.rate,
      current_rate: current.rate,
      drop_pct: dropPct,
    });
  }

  return new Response(
    JSON.stringify({ ok: true, checked: overrides?.length ?? 0, results }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
