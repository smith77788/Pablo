// ACOS Iter-10 — Growth Hunter agent: Auto SEO loop closure.
// Runs weekly via pg_cron. Closes the full SEO loop without admin intervention:
//   1. Picks top-N dead pages from page_stats (views_total >= 50, purchases_total = 0)
//      that don't already have a running experiment AND weren't auto-tested in last 30 days.
//   2. For each, calls Lovable AI Gateway to generate H1/meta/keywords B-variant.
//   3. Creates seo_experiments row (status=running) with current page SEO as variant A
//      and AI suggestion as variant B.
//   4. Logs an ai_insight summarising the autonomous decision.
// Iter-9 rollback monitor + Iter-7 evaluator complete the cycle.
// SAFE: only touches seo_experiments + ai_insights. Never checkout/payment/auth.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAI } from "../_shared/ai-router.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// LOWERED THRESHOLDS — site is young, agent must work even on small traffic.
// Was: 5 / 50 / 30 — too high, agent never fired.
const MAX_PAGES_PER_RUN = 20;
const MIN_VIEWS_FOR_DEAD = 5;
const RECENT_TEST_DAYS = 14;

const SYSTEM_PROMPT = `You are an SEO conversion optimization expert for a Ukrainian pet treats e-commerce store (BASIC.FOOD — dried beef treats for dogs/cats).
Your job: rewrite H1 and meta description for a "dead page" — one with traffic but zero conversions.
Rules:
- Always Ukrainian
- H1: max 60 chars, 1-2 buy-intent keywords (купити, замовити, ціна), benefit-driven
- Meta description: 140-160 chars, include CTA + USP (натуральне, без хімії, доставка по Україні)
- Keywords: 5-8 commercial-intent Ukrainian keywords
Respond ONLY via the suggest_seo_rewrite tool.`;

interface DeadPage {
  page_path: string;
  views_total: number;
}

interface SeoSuggestion {
  h1: string;
  meta_description: string;
  meta_title: string;
  keywords: string[];
}

const generateSuggestion = async (page: DeadPage): Promise<SeoSuggestion | null> => {
  const userPrompt = `Сторінка: ${page.page_path}\nПереглядів: ${page.views_total}\nПокупок: 0 (DEAD PAGE)\nЗгенеруй buy-intent оптимізований H1 + meta_title + meta_description + keywords. Підлаштуй під тип URL (/catalog, /product/[slug], /promotions тощо).`;

  try {
    const result = await routeAI({
      model: "google/gemini-2.5-flash",
      messages: [
        { role: "system", content: SYSTEM_PROMPT },
        { role: "user", content: userPrompt },
      ],
      tools: [
        {
          type: "function",
          function: {
            name: "suggest_seo_rewrite",
            description: "Return optimized SEO H1, meta_title, meta_description and keywords",
            parameters: {
              type: "object",
              properties: {
                h1: { type: "string", description: "New H1 in Ukrainian, max 60 chars" },
                meta_title: { type: "string", description: "Meta title in Ukrainian, max 60 chars" },
                meta_description: { type: "string", description: "Meta description in Ukrainian, 140-160 chars" },
                keywords: { type: "array", items: { type: "string" }, description: "5-8 buy-intent UA keywords" },
              },
              required: ["h1", "meta_title", "meta_description", "keywords"],
              additionalProperties: false,
            },
          },
        },
      ],
      tool_choice: { type: "function", function: { name: "suggest_seo_rewrite" } },
    });

    const args = result.toolCalls?.[0]?.function?.arguments ?? result.content;
    if (!args) return null;
    return JSON.parse(args) as SeoSuggestion;
  } catch (e) {
    console.warn(`[autonomous-seo-loop] AI failed for ${page.page_path}:`, e);
    return null;
  }
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-autonomous-seo-loop", req);

  const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
  const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

  const supabase = createClient(SUPABASE_URL, SERVICE_KEY);

  // 1. Find dead pages.
  const { data: deadPages, error: deadErr } = await supabase
    .from("page_stats")
    .select("page_path, views_total, purchases_total")
    .gte("views_total", MIN_VIEWS_FOR_DEAD)
    .eq("purchases_total", 0)
    .order("views_total", { ascending: false })
    .limit(50);

  if (deadErr) {
    return new Response(JSON.stringify({ ok: false, error: deadErr.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (!deadPages || deadPages.length === 0) {
    return new Response(JSON.stringify({ ok: true, message: "No dead pages found", processed: 0 }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // 2. Filter out pages with running experiments OR tested in last 30d.
  const cutoff = new Date(Date.now() - RECENT_TEST_DAYS * 24 * 3600 * 1000).toISOString();
  const deadPaths = deadPages.map((dp) => dp.page_path).filter(Boolean) as string[];
  const { data: coveredExps } = await supabase
    .from("seo_experiments")
    .select("page_path")
    .in("page_path", deadPaths)
    .or(`status.eq.running,created_at.gte.${cutoff}`);
  const coveredPaths = new Set((coveredExps ?? []).map((e) => e.page_path));
  const candidates: DeadPage[] = [];
  for (const dp of deadPages) {
    if (!dp.page_path || coveredPaths.has(dp.page_path)) continue;
    candidates.push({ page_path: dp.page_path, views_total: dp.views_total ?? 0 });
    if (candidates.length >= MAX_PAGES_PER_RUN) break;
  }

  if (candidates.length === 0) {
    return new Response(JSON.stringify({ ok: true, message: "All dead pages already covered", processed: 0 }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // 3. Pre-fetch seo_overrides for all candidate pages in one batch.
  const candidatePaths = candidates.map((c) => c.page_path);
  const { data: overrideRows } = await supabase
    .from("seo_overrides")
    .select("page_path, h1, meta_title, meta_description, keywords")
    .in("page_path", candidatePaths);
  const overrideMap = new Map(
    (overrideRows ?? []).map((r: any) => [r.page_path, r]),
  );

  // Generate AI suggestions + create experiments.
  const results: Array<Record<string, unknown>> = [];
  const insightRows: object[] = [];
  for (const page of candidates) {
    const suggestion = await generateSuggestion(page);
    if (!suggestion) {
      results.push({ page_path: page.page_path, action: "skip", reason: "ai_failed" });
      continue;
    }

    // Variant A = current overrides if any, else nulls (PageSeo falls back to hardcoded).
    const currentOverride = overrideMap.get(page.page_path) ?? null;

    const { data: exp, error: expErr } = await supabase
      .from("seo_experiments")
      .insert({
        page_path: page.page_path,
        status: "running",
        variant_a_h1: currentOverride?.h1 ?? null,
        variant_a_meta_title: currentOverride?.meta_title ?? null,
        variant_a_meta_description: currentOverride?.meta_description ?? null,
        variant_a_keywords: currentOverride?.keywords ?? [],
        variant_b_h1: suggestion.h1,
        variant_b_meta_title: suggestion.meta_title,
        variant_b_meta_description: suggestion.meta_description,
        variant_b_keywords: suggestion.keywords,
      })
      .select("id")
      .single();

    if (expErr || !exp) {
      results.push({ page_path: page.page_path, action: "create_failed", error: expErr?.message });
      continue;
    }

    insightRows.push({
      insight_type: "autonomous_seo_loop",
      affected_layer: "seo",
      risk_level: "low",
      title: `Auto A/B started: ${page.page_path}`,
      description: `Growth Hunter автоматично знайшов dead page (${page.views_total} переглядів, 0 покупок) і запустив A/B тест.\n\nВаріант B (AI):\nH1: «${suggestion.h1}»\nMeta: «${suggestion.meta_description}»`,
      expected_impact: `Очікується конверсія 1-3% (~${Math.round(page.views_total * 0.02)} продажів за період).`,
      confidence: 0.7,
      metrics: {
        page_path: page.page_path,
        experiment_id: exp.id,
        views_before: page.views_total,
        suggestion,
        autonomous: true,
      },
      status: "new",
    });

    results.push({ page_path: page.page_path, action: "experiment_created", experiment_id: exp.id });
  }

  if (insightRows.length > 0) {
    await supabase.from("ai_insights").insert(insightRows);
  }

  __agent.success();
    return new Response(
    JSON.stringify({ ok: true, processed: results.length, results }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
