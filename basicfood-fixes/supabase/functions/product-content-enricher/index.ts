// ProductContentEnricher — motor neuron of the BASIC.FOOD organism.
//
// Closes the loop opened by ProductConversionAnalyzer: that interneuron
// flagged 5 products with high views and ≤5% view→cart conversion (Аорта,
// Жила, Пеніс, Трахея, Вим'я). Their descriptions are 222–388 chars vs the
// 426-char top performer (Легені, 56% conversion). Plain content gap.
//
// What this neuron does:
//   1. Pulls the latest ProductConversionAnalyzer report from debug_reports.
//   2. For each suspect, asks Lovable AI Gateway to draft an enriched
//      Ukrainian description, given the brand voice + the top performer as
//      a stylistic anchor + a hard list of forbidden phrases sourced from
//      mem://features/product-composition.
//   3. Writes every draft into content_proposals as `pending` — never
//      mutates products directly. A human approves in admin → a separate
//      step applies. This keeps the network honest and auditable.
//
// Why a separate proposal table (not direct UPDATE):
//   product description is customer-facing brand surface. An LLM should
//   propose, never publish. The BrandComplianceSentinel exists precisely
//   because manually-written copy already drifted; auto-publishing LLM
//   copy would amplify that risk.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";
import { routeAIText } from "../_shared/ai-router.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const FORBIDDEN_PHRASES = [
  "100% м'яса", "100% мʼяса", "100% м'ясо", "100% мʼясо",
  "100% натуральне м'ясо", "100% натуральне мʼясо",
  "100% beef", "pure beef", "pure meat", "pure muscle meat",
  "чиста українська яловичина", "чисте м'ясо", "чисте мʼясо",
  "1 інгредієнт — м'ясо", "1 інгредієнт — мʼясо",
];

interface Suspect {
  product_id: string;
  name: string;
  views: number;
  adds: number;
  conversion_pct: number | null;
  description_len: number;
}

interface TopPerformer {
  name: string;
  conversion_pct: number | null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;
  const triggerBody = await req.clone().json().catch(() => ({}));

  return await withAgentRun("product-content-enricher", detectTrigger(req, triggerBody), async () => {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );
    // AI Router handles provider selection (Together → Cohere → Google AI →
    // Groq → OpenRouter → Lovable). When Lovable Gateway returns 402 the
    // router silently falls back to a free provider, so this neuron keeps
    // producing drafts even with zero AI Gateway credits.


  // 1. Latest conversion report. We only act on what the analyzer found —
  // this neuron has no opinion of its own about which products are weak.
  const { data: report } = await supabase
    .from("debug_reports")
    .select("context, created_at")
    .eq("source", "product-conversion-analyzer")
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();

  const ctx = (report?.context ?? {}) as any;
  const suspects: Suspect[] = ctx.suspects ?? [];
  const topPerformer: TopPerformer | undefined = (ctx.top_performers ?? [])[0];

    if (suspects.length === 0) {
      return {
        result: new Response(JSON.stringify({ ok: true, skipped: "no_suspects" }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }),
        summary: "no_suspects",
        payload: { drafts: 0 },
        status: "success" as const,
      };
    }


  // 2. Avoid duplicate proposals — skip products that already have a pending
  // draft so re-running the cron doesn't spam admins.
  const { data: existing } = await supabase
    .from("content_proposals")
    .select("product_id")
    .in("product_id", suspects.map((s) => s.product_id))
    .eq("status", "pending")
    .eq("field", "description");
  const skip = new Set((existing ?? []).map((r: any) => r.product_id));

  // 3. Fetch full source rows for suspects + the top performer (as anchor).
  const allIds = [...new Set([...suspects.map((s) => s.product_id), ...((ctx.top_performers ?? []) as any[]).map((t) => t.product_id).filter(Boolean)])];
  const { data: products } = await supabase
    .from("products")
    .select("id, name, price, description, composition")
    .in("id", allIds);
  const byId = new Map((products ?? []).map((p: any) => [p.id, p]));
  const anchor = topPerformer
    ? (products ?? []).find((p: any) => p.name === topPerformer.name)
    : undefined;

  const drafts: Array<{ product: string; status: string; reason?: string }> = [];
  const proposalBatch: Array<{ productName: string; row: object }> = [];

  for (const s of suspects) {
    if (skip.has(s.product_id)) {
      drafts.push({ product: s.name, status: "skipped_pending_exists" });
      continue;
    }
    const p = byId.get(s.product_id);
    if (!p) {
      drafts.push({ product: s.name, status: "skipped_not_found" });
      continue;
    }

    const systemPrompt = [
      "Ти — копірайтер бренду BASIC.FOOD: натуральні сушені ласощі для собак і котів.",
      "ОБОВʼЯЗКОВО: продукт — це 100% яловичі субпродукти найвищої якості (НЕ мʼясо).",
      "ЗАБОРОНЕНО використовувати ці фрази дослівно або в схожій формі:",
      ...FORBIDDEN_PHRASES.map((f) => `  – «${f}»`),
      "Замість «мʼясо» пиши «субпродукт». Якщо потрібно описати смак, можна «мʼясний смак/аромат» — це характеристика, а не склад.",
      "Стиль: тепло, конкретно, без води. 380–460 символів. UA. Дозволено 1 емодзі на початку.",
      "Структура (одним абзацом, без заголовків): СЕО-гачок «Купити...» → 1-2 функціональні переваги для тваринки → 2-3 факти про користь субпродукту → склад/походження (Україна, без хімії) → CTA з доставкою.",
      "Поточний рік: 2026.",
    ].join("\n");

    const userPrompt = [
      `Товар: ${p.name}`,
      `Ціна: ${p.price} грн`,
      `Поточний склад: ${p.composition ?? "—"}`,
      `Поточний опис (${(p.description ?? "").length} симв., конверсія ${s.conversion_pct ?? 0}%):`,
      p.description ?? "(відсутній)",
      "",
      anchor ? `Стилістичний еталон — товар «${anchor.name}» (конверсія ${topPerformer?.conversion_pct}%):` : "",
      anchor ? anchor.description ?? "" : "",
      "",
      "Перепиши опис цього товару в стилі еталону, з урахуванням ВСІХ заборон. Поверни лише фінальний текст опису, без коментарів.",
    ].filter(Boolean).join("\n");

    let proposed = "";
    try {
      proposed = (await routeAIText({
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: userPrompt },
        ],
        model: "google/gemini-2.5-flash",
        temperature: 0.7,
        max_tokens: 700,
        skipLovable: false, // allow Lovable as last-resort fallback
        noCache: true,      // each product needs a unique draft
      })).trim();
    } catch (e: any) {
      drafts.push({ product: s.name, status: "ai_failed", reason: (e?.message ?? String(e)).slice(0, 200) });
      continue;
    }
    if (!proposed || proposed.length < 200) {
      drafts.push({ product: s.name, status: "ai_too_short" });
      continue;
    }

    // Post-LLM safety net: if a forbidden phrase slipped through, reject the
    // draft instead of saving it. The sentinel would catch it later but we
    // prefer to never let it touch the proposals table.
    const lower = proposed.toLowerCase();
    const violation = FORBIDDEN_PHRASES.find((f) => lower.includes(f.toLowerCase()));
    if (violation) {
      drafts.push({ product: s.name, status: "rejected_forbidden_phrase", reason: violation });
      continue;
    }

    proposalBatch.push({
      productName: s.name,
      row: {
        product_id: p.id,
        field: "description",
        current_value: p.description,
        proposed_value: proposed,
        reason: `Conversion ${s.conversion_pct}% on ${s.views} views (target ≥30%). Description was ${s.description_len} chars vs ${anchor ? (anchor.description ?? "").length : "?"} on top performer.`,
        source_neuron: "ProductContentEnricher",
        model: "google/gemini-2.5-flash",
        metadata: {
          analyzer_views: s.views,
          analyzer_adds: s.adds,
          analyzer_conv_pct: s.conversion_pct,
          anchor_product: anchor?.name,
        },
      },
    });
  }

  // Batch insert all proposal rows in one DB round-trip.
  if (proposalBatch.length > 0) {
    const { error: batchErr } = await supabase
      .from("content_proposals")
      .insert(proposalBatch.map((b) => b.row));
    if (batchErr) {
      for (const b of proposalBatch) {
        drafts.push({ product: b.productName, status: "insert_failed", reason: batchErr.message });
      }
    } else {
      for (const b of proposalBatch) {
        drafts.push({ product: b.productName, status: "proposal_created" });
      }
    }
  }

    const created = drafts.filter((d) => d.status === "proposal_created").length;
    const aiFailed = drafts.filter((d) => d.status === "ai_failed").length;
    return {
      result: new Response(JSON.stringify({ ok: true, drafts }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }),
      summary: `created=${created} ai_failed=${aiFailed} suspects=${suspects.length}`,
      payload: { created, ai_failed: aiFailed, suspects: suspects.length },
      status: aiFailed > 0 ? ("partial" as const) : ("success" as const),
    };
  });
});

