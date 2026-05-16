// ACOS Event Processor — analyzes incoming events and writes insights.
// Iteration #22: Lovable AI Gateway (gemini-2.5-flash) for semantic analysis,
// supplemented by deterministic rule-based detection as a safety net.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAI } from "../_shared/ai-router.ts";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

interface EventPayload {
  event_id: string;
  event_type: string;
  user_id: string | null;
  session_id: string | null;
  product_id: string | null;
  order_id: string | null;
  metadata: Record<string, unknown>;
  source: string;
}

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");

// ─────────────────────────────────────────────────────────────────────────────
// Rule-based safety net (always runs, throttled)
// ─────────────────────────────────────────────────────────────────────────────
async function maybeAnalyzeFunnel(): Promise<void> {
  if (Math.random() > 0.04) return;

  const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const steps = ["product_viewed", "add_to_cart", "begin_checkout", "purchase_completed"] as const;
  const stepResults = await Promise.all(
    steps.map((step) =>
      supabase.from("events").select("*", { count: "exact", head: true })
        .eq("event_type", step).gte("created_at", since)
    ),
  );
  const stepCounts: Record<string, number> = Object.fromEntries(
    steps.map((step, i) => [step, stepResults[i].count ?? 0]),
  );

  if (stepCounts.product_viewed < 20) return;

  const steps = [
    { from: "product_viewed", to: "add_to_cart", label: "PDP→Cart" },
    { from: "add_to_cart", to: "begin_checkout", label: "Cart→Checkout" },
    { from: "begin_checkout", to: "purchase_completed", label: "Checkout→Purchase" },
  ];

  let worst = { label: "", dropPct: 0, from: 0, to: 0 };
  for (const s of steps) {
    const fromN = stepCounts[s.from];
    const toN = stepCounts[s.to];
    if (fromN === 0) continue;
    const dropPct = Math.round(((fromN - toN) / fromN) * 100);
    if (dropPct > worst.dropPct) {
      worst = { label: s.label, dropPct, from: fromN, to: toN };
    }
  }
  if (worst.dropPct < 50) return;

  // DB-level dedup (unique index on insight_type + hourly bucket) is the source of truth.
  // Application check removed — race conditions handled by Postgres now.

  // Insert; if DB unique constraint (insight_type + hourly bucket) blocks it, ignore silently.
  const { error: insertErr } = await supabase.from("ai_insights").insert({
    insight_type: "funnel_bottleneck",
    title: `Найбільший провал у воронці: ${worst.label} (${worst.dropPct}% drop)`,
    description: `За останні 24 год ${worst.from} подій ${worst.label.split("→")[0]} → лише ${worst.to} ${worst.label.split("→")[1]}. Це втрата ${worst.dropPct}% користувачів на цьому кроці. Рекомендується A/B-тест зменшення friction.`,
    expected_impact: `+${Math.round(worst.dropPct * 0.15)}% conversion at this step`,
    confidence: stepCounts.product_viewed > 100 ? 0.85 : 0.6,
    affected_layer: worst.label.includes("PDP") ? "website" : worst.label.includes("Checkout") ? "checkout" : "cart",
    risk_level: "low",
    metrics: { window_hours: 24, step_counts: stepCounts, drop_pct: worst.dropPct, from_count: worst.from, to_count: worst.to, source: "rule_based" },
  });
  if (insertErr && insertErr.code !== "23505") {
    console.error("[funnel] insert failed:", insertErr.message);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// AI-powered semantic analysis (Lovable AI Gateway, gemini-2.5-flash)
// Throttled to ~1/50 events to control cost.
// ─────────────────────────────────────────────────────────────────────────────
async function maybeAnalyzeWithAI(): Promise<void> {
  if (!LOVABLE_API_KEY) return;
  if (Math.random() > 0.02) return;

  // Don't spam: skip if AI insight created in last 2h
  const { data: recent } = await supabase
    .from("ai_insights")
    .select("id")
    .like("insight_type", "ai_%")
    .gte("created_at", new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString())
    .limit(1);
  if (recent && recent.length > 0) return;

  // Gather context: last 200 events + funnel summary + top products
  const since24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const { data: events } = await supabase
    .from("events")
    .select("event_type, product_id, metadata, created_at")
    .gte("created_at", since24h)
    .order("created_at", { ascending: false })
    .limit(200);

  if (!events || events.length < 30) return; // need minimum signal

  // Aggregate
  const byType: Record<string, number> = {};
  const productViews: Record<string, number> = {};
  for (const e of events) {
    byType[e.event_type] = (byType[e.event_type] ?? 0) + 1;
    if (e.event_type === "product_viewed" && e.product_id) {
      productViews[e.product_id] = (productViews[e.product_id] ?? 0) + 1;
    }
  }

  const topProductIds = Object.entries(productViews)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([id]) => id);

  let topProducts: Array<{ id: string; name: string; price: number; views: number }> = [];
  if (topProductIds.length > 0) {
    const { data: prods } = await supabase
      .from("products")
      .select("id, name, price")
      .in("id", topProductIds);
    topProducts = (prods ?? []).map((p) => ({ ...p, views: productViews[p.id] ?? 0 }));
  }

  const context = {
    window_hours: 24,
    total_events: events.length,
    events_by_type: byType,
    top_viewed_products: topProducts,
    sample_metadata: events.slice(0, 10).map((e) => ({ type: e.event_type, meta: e.metadata })),
  };

  const systemPrompt = `Ти — ACOS (Autonomous Commerce Optimization System). Аналізуєш події e-commerce магазину BASIC.FOOD (сушене м'ясо для тварин, Україна).
Знайди 1 найважливіший інсайт на основі даних. Будь конкретним, з цифрами. Українською.
Категорії інсайтів: ai_conversion (можливість підняти конверсію), ai_retention (ризик відтоку), ai_upsell (можливість допродажу), ai_product (інсайт про конкретний продукт).
Не вигадуй дані — тільки те, що видно у вхідному JSON.`;

  const userPrompt = `Дані за 24 години:\n${JSON.stringify(context, null, 2)}\n\nЗгенеруй 1 інсайт у форматі JSON: { "insight_type": "ai_*", "title": "...", "description": "...", "expected_impact": "...", "confidence": 0.0-1.0, "affected_layer": "website|cart|checkout|bot", "risk_level": "low|medium|high" }`;

  let aiResult: any;
  try {
    aiResult = await routeAI({
      model: "google/gemini-2.5-flash",
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userPrompt },
      ],
      response_format: { type: "json_object" },
    });
  } catch (err) {
    console.error("AI router failed", err);
    return;
  }

  const content = aiResult?.content;
  if (!content) return;

  let parsed: any;
  try {
    parsed = JSON.parse(content);
  } catch {
    console.error("AI returned non-JSON", content);
    return;
  }

  if (!parsed?.title || !parsed?.description || !parsed?.insight_type) return;

  await supabase.from("ai_insights").insert({
    insight_type: String(parsed.insight_type).slice(0, 64),
    title: String(parsed.title).slice(0, 255),
    description: String(parsed.description).slice(0, 2000),
    expected_impact: parsed.expected_impact ? String(parsed.expected_impact).slice(0, 255) : null,
    confidence: Math.max(0, Math.min(1, Number(parsed.confidence) || 0.6)),
    affected_layer: parsed.affected_layer ?? null,
    risk_level: ["low", "medium", "high"].includes(parsed.risk_level) ? parsed.risk_level : "low",
    metrics: { ...context, source: "lovable_ai_gemini_2_5_flash" },
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Upsell underperformance (rule-based, kept as safety net)
// ─────────────────────────────────────────────────────────────────────────────
async function maybeAnalyzeUpsells(eventType: string): Promise<void> {
  if (eventType !== "offer_shown") return;
  if (Math.random() > 0.02) return;

  const since = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
  const [{ count: shown }, { count: accepted }] = await Promise.all([
    supabase.from("events").select("*", { count: "exact", head: true })
      .eq("event_type", "offer_shown").gte("created_at", since),
    supabase.from("events").select("*", { count: "exact", head: true })
      .eq("event_type", "upsell_accepted").gte("created_at", since),
  ]);

  if ((shown ?? 0) < 50) return;
  const acceptRate = ((accepted ?? 0) / (shown ?? 1)) * 100;
  if (acceptRate >= 5) return;

  const { data: existing } = await supabase
    .from("ai_insights")
    .select("id")
    .eq("insight_type", "upsell_underperforming")
    .gte("created_at", new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString())
    .limit(1);
  if (existing && existing.length > 0) return;

  await supabase.from("ai_insights").insert({
    insight_type: "upsell_underperforming",
    title: `Upsell offers конвертують лише ${acceptRate.toFixed(1)}%`,
    description: `За 7 днів показано ${shown} офферів, прийнято ${accepted}. Це нижче бенчмарку 5%. Розглянь: інший копірайтинг, кращий visual, релевантніший продукт, або кращий timing.`,
    expected_impact: "+15-30% upsell revenue",
    confidence: 0.75,
    affected_layer: "website",
    risk_level: "low",
    metrics: { shown, accepted, accept_rate_pct: acceptRate, window_days: 7, source: "rule_based" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  // Event firehose from every page — 120 req/min sustained, burst 120.
  // Tracking pixels and add-to-cart fire often, so this is more permissive.
  const rl = rateLimit(`event-processor:${getClientIp(req)}`, { capacity: 120, refillPerSec: 2 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  // Health-check / cron-friendly GET — runs analyzers without an event payload
  if (req.method === "GET") {
    await Promise.allSettled([maybeAnalyzeFunnel(), maybeAnalyzeWithAI()]);
    return new Response(JSON.stringify({ ok: true, mode: "health" }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
      status: 200,
    });
  }

  try {
    // Tolerate empty/invalid bodies — analyzers can still run.
    let payload: EventPayload | null = null;
    try {
      const raw = await req.text();
      if (raw && raw.trim().length > 0) payload = JSON.parse(raw) as EventPayload;
    } catch {
      payload = null;
    }

    await Promise.allSettled([
      maybeAnalyzeFunnel(),
      payload?.event_type ? maybeAnalyzeUpsells(payload.event_type) : Promise.resolve(),
      maybeAnalyzeWithAI(),
    ]);

    return new Response(JSON.stringify({ ok: true, processed: !!payload }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
      status: 200,
    });
  } catch (err) {
    console.error("event-processor error", err);
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
      status: 500,
    });
  }
});
