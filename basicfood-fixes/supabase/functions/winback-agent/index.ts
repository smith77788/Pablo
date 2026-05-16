// Customer Win-Back Agent — RFM segmentation + AI-generated personal outreach.
//
// Runs weekly. For each unique buyer (by phone) computes:
//   R = days since last order
//   F = total completed orders
//   M = lifetime monetary (sum of totals)
// Buckets into segments: champions / loyal / potential / new / at_risk / hibernating / lost.
// For non-active segments (at_risk, hibernating, lost) generates a personalized
// Ukrainian win-back message via AI.
// Persists to customer_segments (UPSERT by phone). Emits 1 ai_insight summary.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAIText } from "../_shared/ai-router.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

type Segment =
  | "champions" | "loyal" | "potential" | "new"
  | "at_risk" | "hibernating" | "lost";

interface CustomerRFM {
  phone: string;
  name: string | null;
  email: string | null;
  user_id: string | null;
  recency_days: number;
  frequency: number;
  monetary: number;
  first_order_at: string;
  last_order_at: string;
}

function classify(c: CustomerRFM): { segment: Segment; rfm_score: string } {
  const r = c.recency_days <= 30 ? 5 : c.recency_days <= 60 ? 4 : c.recency_days <= 120 ? 3 : c.recency_days <= 240 ? 2 : 1;
  const f = c.frequency >= 6 ? 5 : c.frequency >= 4 ? 4 : c.frequency >= 3 ? 3 : c.frequency === 2 ? 2 : 1;
  const m = c.monetary >= 500000 ? 5 : c.monetary >= 200000 ? 4 : c.monetary >= 100000 ? 3 : c.monetary >= 50000 ? 2 : 1;
  const score = `${r}${f}${m}`;

  let segment: Segment;
  if (r >= 4 && f >= 4) segment = "champions";
  else if (r >= 3 && f >= 3) segment = "loyal";
  else if (r >= 4 && f <= 2) segment = c.frequency === 1 ? "new" : "potential";
  else if (r === 3 && f <= 2) segment = "potential";
  else if (r === 2 && f >= 3) segment = "at_risk";
  else if (r === 2) segment = "hibernating";
  else if (r === 1 && f >= 3) segment = "at_risk";
  else if (r === 1) segment = "lost";
  else segment = "potential";

  return { segment, rfm_score: score };
}

const OFFER_PLAYBOOK: Record<Segment, { offer: string; channel: string }> = {
  champions:   { offer: "Ексклюзивна новинка / ранній доступ",       channel: "telegram" },
  loyal:       { offer: "Бонус +5% кешбеку на наступне замовлення",   channel: "telegram" },
  potential:   { offer: "Промо -10% на другу позицію в кошику",       channel: "telegram" },
  new:         { offer: "Welcome-набір + інструкція по підбору",      channel: "telegram" },
  at_risk:     { offer: "Персональна знижка -15% на 7 днів",          channel: "telegram" },
  hibernating: { offer: "Знижка -20% + безкоштовна доставка від 800₴", channel: "sms" },
  lost:        { offer: "Останній шанс: -25% + подарунок до замовлення", channel: "sms" },
};

const SYSTEM = `Ти — копірайтер BASIC.FOOD (натуральні сушені ласощі для собак з яловичих субпродуктів).
Тон: теплий, дружній, на "ти", без агресивного продажу. Українською. Без емодзі-спаму (макс 1).
Пиши коротко (2-4 речення, до 280 символів) — це повідомлення в Telegram/SMS.
Звертайся по імені якщо є. Згадай конкретний минулий товар. Заклич повернутися. Включи offer.
ЗАБОРОНЕНО: фрази "100% м'яса", "100% beef" — це субпродукти.`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    // Pull all completed orders with phone (revenue-bearing statuses only)
    const { data: orders, error } = await supabase
      .from("orders")
      .select("customer_phone, customer_name, customer_email, user_id, total, created_at, id")
      .in("status", ["completed", "shipped", "delivered", "paid"])
      .not("customer_phone", "is", null)
      .order("created_at", { ascending: false })
      .limit(5000);
    if (error) throw error;

    if (!orders || orders.length === 0) {
      return new Response(JSON.stringify({ ok: true, skipped: true, reason: "no_orders" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } });
    }

    // Aggregate by phone
    const grouped = new Map<string, CustomerRFM & { last_product?: string }>();
    for (const o of orders) {
      const phone = (o.customer_phone || "").trim();
      if (!phone) continue;
      const existing = grouped.get(phone);
      const ts = new Date(o.created_at).getTime();
      if (existing) {
        existing.frequency += 1;
        existing.monetary += o.total ?? 0;
        if (ts > new Date(existing.last_order_at).getTime()) existing.last_order_at = o.created_at;
        if (ts < new Date(existing.first_order_at).getTime()) existing.first_order_at = o.created_at;
      } else {
        grouped.set(phone, {
          phone,
          name: o.customer_name,
          email: o.customer_email,
          user_id: o.user_id,
          recency_days: 0,
          frequency: 1,
          monetary: o.total ?? 0,
          first_order_at: o.created_at,
          last_order_at: o.created_at,
        });
      }
    }

    // Last bought product per phone (for personalization)
    const orderIds = orders.slice(0, 200).map(o => o.id);
    const { data: items } = await supabase
      .from("order_items")
      .select("order_id, product_name")
      .in("order_id", orderIds);
    const orderToProduct = new Map<string, string>();
    for (const it of items ?? []) {
      if (!orderToProduct.has(it.order_id)) orderToProduct.set(it.order_id, it.product_name);
    }
    // most recent order per phone -> product
    const phoneToLastProduct = new Map<string, string>();
    for (const o of orders) {
      const phone = (o.customer_phone || "").trim();
      if (!phone || phoneToLastProduct.has(phone)) continue;
      const p = orderToProduct.get(o.id);
      if (p) phoneToLastProduct.set(phone, p);
    }

    const now = Date.now();
    const customers = Array.from(grouped.values()).map(c => ({
      ...c,
      recency_days: Math.floor((now - new Date(c.last_order_at).getTime()) / 86400000),
    }));

    const segCounts: Record<string, number> = {};
    const upserts: any[] = [];
    const winBackTargets: Array<{ c: CustomerRFM; segment: Segment; lastProduct?: string }> = [];

    for (const c of customers) {
      const { segment, rfm_score } = classify(c);
      segCounts[segment] = (segCounts[segment] ?? 0) + 1;
      const playbook = OFFER_PLAYBOOK[segment];
      const lastProduct = phoneToLastProduct.get(c.phone);

      const row: any = {
        customer_phone: c.phone,
        customer_name: c.name,
        customer_email: c.email,
        user_id: c.user_id,
        segment,
        recency_days: c.recency_days,
        frequency: c.frequency,
        monetary: c.monetary,
        rfm_score,
        last_order_at: c.last_order_at,
        first_order_at: c.first_order_at,
        ai_offer: playbook.offer,
        recommended_channel: playbook.channel,
        computed_at: new Date().toISOString(),
      };

      if (segment === "at_risk" || segment === "hibernating" || segment === "lost") {
        winBackTargets.push({ c, segment, lastProduct });
      }
      upserts.push(row);
    }

    // Generate AI messages for win-back targets (cap at 30 per run to control cost)
    const aiTargets = winBackTargets.slice(0, 30);
    const messageByPhone = new Map<string, string>();

    for (const t of aiTargets) {
      const userPrompt =
        `Сегмент: ${t.segment}\n` +
        `Імʼя: ${t.c.name ?? "невідомо"}\n` +
        `Днів з останнього замовлення: ${t.c.recency_days}\n` +
        `Кількість замовлень: ${t.c.frequency}, LTV: ${(t.c.monetary/100).toFixed(0)}₴\n` +
        `Останній товар: ${t.lastProduct ?? "—"}\n` +
        `Offer: ${OFFER_PLAYBOOK[t.segment].offer}\n` +
        `Канал: ${OFFER_PLAYBOOK[t.segment].channel}\n\n` +
        `Напиши коротке повідомлення (2-4 речення, до 280 символів).`;

      try {
        const text = await routeAIText({
          model: "google/gemini-2.5-flash",
          skipLovable: false,
          messages: [
            { role: "system", content: SYSTEM },
            { role: "user", content: userPrompt },
          ],
        });
        const clean = text.trim().slice(0, 320);
        messageByPhone.set(t.c.phone, clean);
      } catch (e) {
        console.error("AI gen failed for", t.c.phone, e);
      }
    }

    // Attach messages
    for (const u of upserts) {
      const m = messageByPhone.get(u.customer_phone);
      if (m) u.ai_message = m;
    }

    // Upsert in parallel chunks
    const CHUNK = 200;
    const upsertChunks: typeof upserts[] = [];
    for (let i = 0; i < upserts.length; i += CHUNK) upsertChunks.push(upserts.slice(i, i + CHUNK));
    const upsertResults = await Promise.all(
      upsertChunks.map((chunk) =>
        supabase.from("customer_segments").upsert(chunk, { onConflict: "customer_phone" })
      ),
    );
    for (const { error: upErr } of upsertResults) { if (upErr) throw upErr; }

    const winBackCount = (segCounts["at_risk"] ?? 0) + (segCounts["hibernating"] ?? 0) + (segCounts["lost"] ?? 0);

    // Insight summary
    const fingerprint = `winback_${new Date().toISOString().slice(0, 10)}`;
    const description = `**RFM сегментація: ${customers.length} клієнтів**

**Champions:** ${segCounts["champions"] ?? 0} • **Loyal:** ${segCounts["loyal"] ?? 0} • **Potential:** ${segCounts["potential"] ?? 0} • **New:** ${segCounts["new"] ?? 0}

**Цілі для повернення:**
• At Risk: ${segCounts["at_risk"] ?? 0}
• Hibernating: ${segCounts["hibernating"] ?? 0}
• Lost: ${segCounts["lost"] ?? 0}

Згенеровано ${messageByPhone.size} персональних win-back повідомлень. Перейди в /admin/winback щоб переглянути та надіслати.`;

    await supabase.from("ai_insights").insert({
      insight_type: "winback_segmentation",
      affected_layer: "marketing",
      risk_level: winBackCount > 20 ? "high" : winBackCount > 5 ? "medium" : "low",
      title: `Win-Back: ${winBackCount} клієнтів потребують повернення`,
      description,
      expected_impact: winBackCount > 0
        ? `Повернення 15-25% з ${winBackCount} = ${Math.round(winBackCount * 0.2)} замовлень`
        : "Усі клієнти активні — продовжуй retention",
      confidence: 0.8,
      status: winBackCount > 5 ? "new" : "ignored",
      metrics: {
        fingerprint,
        total_customers: customers.length,
        segments: segCounts,
        ai_messages_generated: messageByPhone.size,
        generated_by: "winback-agent",
        generated_at: new Date().toISOString(),
      },
    });

    return new Response(
      JSON.stringify({ ok: true, customers: customers.length, segments: segCounts, ai_messages: messageByPhone.size }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    console.error("winback-agent error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
