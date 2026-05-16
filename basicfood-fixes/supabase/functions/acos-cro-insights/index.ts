// Cycle #19 — CRO Insights Engine
// Reads page_view + purchase events (30 days), computes per-page CR vs site
// baseline, writes cro_recommendations and an aggregated insight.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-cro-insights", req);

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const sinceISO = new Date(Date.now() - 30 * 86_400_000).toISOString();

    const { data: views } = await supabase
      .from("events")
      .select("url")
      .eq("event_type", "page_view")
      .gte("created_at", sinceISO)
      .limit(50_000);

    const { data: purchases } = await supabase
      .from("events")
      .select("url, metadata")
      .eq("event_type", "purchase")
      .gte("created_at", sinceISO)
      .limit(10_000);

    const path = (u: string | null | undefined) => {
      if (!u) return "";
      try { return new URL(u).pathname; } catch { return u; }
    };

    const viewMap = new Map<string, number>();
    for (const v of views ?? []) {
      const p = path(v.url as string);
      if (!p) continue;
      viewMap.set(p, (viewMap.get(p) ?? 0) + 1);
    }
    const convMap = new Map<string, number>();
    for (const p of purchases ?? []) {
      // Purchases happen on /order/success; allocate by referrer if present
      const ref = (p.metadata as any)?.referrer ?? p.url;
      const pp = path(ref);
      if (!pp) continue;
      convMap.set(pp, (convMap.get(pp) ?? 0) + 1);
    }

    const totalViews = Array.from(viewMap.values()).reduce((a, b) => a + b, 0);
    const totalConv = Array.from(convMap.values()).reduce((a, b) => a + b, 0);
    const baselineCR = totalViews ? totalConv / totalViews : 0.01;

    const rows: any[] = [];
    for (const [page, v] of viewMap.entries()) {
      if (v < 30) continue; // skip low-traffic
      const c = convMap.get(page) ?? 0;
      const cr = v > 0 ? c / v : 0;
      const delta = baselineCR > 0 ? (cr - baselineCR) / baselineCR : 0;
      let severity = "low";
      let action = "monitor";
      let rationale = `CR ${(cr * 100).toFixed(2)}%, baseline ${(baselineCR * 100).toFixed(2)}%`;
      if (cr === 0 && v >= 50) {
        severity = "high"; action = "rewrite_h1_meta";
        rationale = `Жодної конверсії за ${v} переглядів. Перепиши H1/мета та CTA.`;
      } else if (delta < -0.5 && v >= 50) {
        severity = "high"; action = "redesign_above_fold";
        rationale = `CR на ${Math.round(-delta * 100)}% нижче за середній. Переробити блок над фолдом.`;
      } else if (delta < -0.25) {
        severity = "medium"; action = "improve_cta";
        rationale = `CR на ${Math.round(-delta * 100)}% нижче за середній. Підсилити CTA та довіру.`;
      } else if (delta > 0.5) {
        severity = "low"; action = "scale_traffic";
        rationale = `CR на ${Math.round(delta * 100)}% вище. Сторінка-чемпіон, дай більше трафіку.`;
      }

      rows.push({
        page_path: page,
        current_conv_rate: cr,
        baseline_conv_rate: baselineCR,
        delta_pct: delta,
        views_30d: v,
        conversions_30d: c,
        suggested_action: action,
        rationale,
        severity,
        status: "new",
        computed_at: new Date().toISOString(),
      });
    }

    if (rows.length) {
      // Replace fresh batch
      await supabase.from("cro_recommendations").delete().eq("status", "new");
      const chunks: any[][] = [];
      for (let i = 0; i < rows.length; i += 500) chunks.push(rows.slice(i, i + 500));
      await Promise.all(chunks.map((c) => supabase.from("cro_recommendations").insert(c)));
    }

    const high = rows.filter((r) => r.severity === "high").length;
    if (high >= 1) {
      await supabase.from("ai_insights").insert({
        insight_type: "cro_recommendations",
        title: `${high} сторінок з критично низькою конверсією`,
        description: `Аналіз 30-денних подій: baseline CR ${(baselineCR * 100).toFixed(2)}%. Перевірте топ-рекомендації в CRO-панелі.`,
        confidence: 0.75,
        risk_level: high >= 3 ? "high" : "medium",
        affected_layer: "website",
        metrics: { baseline_cr: baselineCR, total_pages: rows.length, high_severity: high },
      });
    }

    __agent.success();
    return new Response(JSON.stringify({ ok: true, pages: rows.length, baseline_cr: baselineCR, high }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    __agent.error(e);
    console.error("cro-insights error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
