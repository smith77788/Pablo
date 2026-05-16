// Cycle #17 — Programmatic SEO Generator
// Reads search_intent_clusters with suggested_action='generate_landing' and
// proposes new programmatic_landing_pages drafts via the Tribunal. The
// enforcer calls back with `from_tribunal=true` to actually insert the row.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAI } from "../_shared/ai-router.ts";
import { enqueueTribunalCase } from "../_shared/tribunal.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const slugify = (s: string) =>
  s.toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .slice(0, 80) || `page-${Date.now()}`;

interface LandingCopy {
  h1: string;
  meta_title: string;
  meta_description: string;
  intro_html: string;
}

async function generateCopy(query: string, productNames: string[]): Promise<LandingCopy> {
  const fallback: LandingCopy = {
    h1: `${query[0]?.toUpperCase()}${query.slice(1)} — підбірка`,
    meta_title: `${query} — Basic Food`,
    meta_description: `Купуйте ${query} на Basic Food. Швидка доставка по Україні.`,
    intro_html: `<p>Підібрали найкращі товари за запитом «${query}». ${productNames.slice(0, 3).join(", ")}.</p>`,
  };

  try {
    const result = await routeAI({
      model: "google/gemini-2.5-flash-lite",
      messages: [
        {
          role: "system",
          content:
            "Ти SEO-копірайтер українського зоомагазину. Поверни JSON: {h1, meta_title (≤60 символів), meta_description (≤155 символів), intro_html (1-2 короткі параграфи з <p>)}.",
        },
        {
          role: "user",
          content: `Запит: "${query}". Товари: ${productNames.slice(0, 6).join("; ")}. Створи лендінг-копію.`,
        },
      ],
      response_format: { type: "json_object" },
    });
    const parsed = JSON.parse(result.content ?? "{}");
    return {
      h1: parsed.h1 || fallback.h1,
      meta_title: parsed.meta_title || fallback.meta_title,
      meta_description: parsed.meta_description || fallback.meta_description,
      intro_html: parsed.intro_html || fallback.intro_html,
    };
  } catch (_) {
    return fallback;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const reqBody = await req.json().catch(() => ({}));
    if (reqBody?.from_tribunal === true) {
      return await persistApprovedLanding(supabase, reqBody);
    }

    const { data: clusters } = await supabase
      .from("search_intent_clusters")
      .select("id, representative_query, member_queries, total_searches")
      .eq("suggested_action", "generate_landing")
      .eq("status", "new")
      .order("total_searches", { ascending: false })
      .limit(5);

    if (!clusters?.length) {
      return new Response(JSON.stringify({ ok: true, queued: 0, reason: "no_clusters" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Batch dedup: check which slugs already exist in one query
    const clusterSlugs = (clusters ?? []).map((c) => slugify(`p-${c.representative_query}`));
    const { data: existingPages } = await supabase
      .from("programmatic_landing_pages")
      .select("slug")
      .in("slug", clusterSlugs);
    const existingSlugs = new Set((existingPages ?? []).map((p: any) => p.slug as string));

    const processedIds: string[] = [];
    const noProductsIds: string[] = [];

    // Process clusters in parallel: product lookups + copy generation + tribunal enqueues
    const caseResults = await Promise.all(
      (clusters ?? []).map(async (c): Promise<{ cluster_id: string; case_id: string; reused: boolean } | null> => {
        const slug = slugify(`p-${c.representative_query}`);
        if (existingSlugs.has(slug)) {
          processedIds.push(c.id);
          return null;
        }

        const { data: products } = await supabase
          .from("products")
          .select("id, name")
          .ilike("name", `%${c.representative_query.split(" ")[0]}%`)
          .eq("is_active", true)
          .limit(12);

        const productNames = (products ?? []).map((p) => p.name);
        const productIds = (products ?? []).map((p) => p.id);
        if (!productIds.length) {
          noProductsIds.push(c.id);
          return null;
        }

        const copy = await generateCopy(c.representative_query, productNames);
        const keywords = Array.from(new Set([
          c.representative_query,
          ...((c.member_queries as string[]) ?? []).slice(0, 5),
        ]));

        const enq = await enqueueTribunalCase({
          source_function: "acos-programmatic-seo-generator",
          category: "seo",
          urgency: "low",
          proposed_change: {
            kind: "programmatic_landing",
            landing: {
              slug,
              source_cluster_id: c.id,
              h1: copy.h1,
              meta_title: copy.meta_title,
              meta_description: copy.meta_description,
              intro_html: copy.intro_html,
              product_ids: productIds,
              keywords,
              status: "draft",
            },
            cluster_id: c.id,
          },
          context: {
            query: c.representative_query,
            total_searches: c.total_searches,
            product_count: productIds.length,
          },
          expected_impact: `Programmatic page for "${c.representative_query}" (${c.total_searches} searches).`,
        });
        return { cluster_id: c.id, case_id: enq.case_id, reused: enq.reused };
      }),
    );

    // Batch status updates
    const statusOps: Promise<any>[] = [];
    if (processedIds.length) {
      statusOps.push(supabase.from("search_intent_clusters").update({ status: "processed" }).in("id", processedIds));
    }
    if (noProductsIds.length) {
      statusOps.push(supabase.from("search_intent_clusters").update({ status: "no_products" }).in("id", noProductsIds));
    }
    if (statusOps.length) await Promise.all(statusOps);

    const cases = caseResults.filter(Boolean) as Array<{ cluster_id: string; case_id: string; reused: boolean }>;
    const queued = cases.length;

    return new Response(JSON.stringify({ ok: true, queued, cases }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("programmatic-seo error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});

async function persistApprovedLanding(
  supabase: any,
  body: { proposed_change?: Record<string, unknown>; case_id?: string },
): Promise<Response> {
  const change = body.proposed_change as
    | { landing?: Record<string, unknown>; cluster_id?: string } | undefined;
  if (!change?.landing) {
    return new Response(
      JSON.stringify({ ok: false, error: "missing_proposed_change" }),
      { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  const { error: insErr } = await supabase
    .from("programmatic_landing_pages")
    .insert(change.landing);
  if (insErr) {
    return new Response(
      JSON.stringify({ ok: false, error: insErr.message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  if (change.cluster_id) {
    await supabase
      .from("search_intent_clusters")
      .update({ status: "processed" })
      .eq("id", change.cluster_id);
  }

  await supabase.from("ai_insights").insert({
    insight_type: "programmatic_seo_generated",
    title: `Tribunal схвалив програматичну сторінку: ${(change.landing as any).slug}`,
    description: `Сторінка створена після перевірки Tribunal (case ${body.case_id ?? "?"}). Перевірте в адмінці перед публікацією.`,
    confidence: 0.8,
    risk_level: "low",
    affected_layer: "seo",
    metrics: { slug: (change.landing as any).slug, tribunal_case_id: body.case_id },
  });

  return new Response(JSON.stringify({ ok: true, case_id: body.case_id }), {
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
