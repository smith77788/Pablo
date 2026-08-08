// ACOS Second-Order Nurture — auto-sends a personalized "NEXT15" promo
// (15% off, min 250₴, valid 14d, single-use) to customers who placed
// EXACTLY ONE order 25-35 days ago and never returned. Targets the
// retention gap surfaced by acos-first-order-funnel.
//
// Cooldown: each customer is touched at most once via this channel
// (event_type=second_order_nurture_sent). Skips churn-paused/tg-blocked.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const TG_BOT_TOKEN = Deno.env.get("TELEGRAM_API_KEY") ?? Deno.env.get("TELEGRAM_API_KEY_1");
const PROMO_DISCOUNT_PCT = 15;
const PROMO_MIN_ORDER = 250;
const PROMO_TTL_DAYS = 14;
const WINDOW_MIN_DAYS = 25;
const WINDOW_MAX_DAYS = 35;
const MAX_BATCH = 50;
const dayMs = 24 * 60 * 60 * 1000;

interface Customer {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  telegram_chat_id: number | null;
  total_orders: number;
  tags: string[];
}

interface OrderRow {
  customer_phone: string | null;
  customer_email: string | null;
  total: number;
  created_at: string;
}

const generateCode = () =>
  "NX15" + Math.random().toString(36).slice(2, 7).toUpperCase();

const sendTelegram = async (chatId: number, text: string): Promise<boolean> => {
  if (!TG_BOT_TOKEN) return false;
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    });
    return res.ok;
  } catch {
    return false;
  }
};

const SKIP_TAGS = new Set(["promo_paused", "tg_blocked", "do_not_contact"]);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));
    const dryRun = body.dry_run === true;

    const now = Date.now();
    const minFirstAt = new Date(now - WINDOW_MAX_DAYS * dayMs).toISOString();
    const maxFirstAt = new Date(now - WINDOW_MIN_DAYS * dayMs).toISOString();

    // 1. Pull every customer with exactly 1 order who has a Telegram chat.
    const { data: customers } = await supabase
      .from("customers")
      .select("id, name, phone, email, telegram_chat_id, total_orders, tags")
      .eq("total_orders", 1)
      .not("telegram_chat_id", "is", null)
      .limit(2000);

    const oneTimers = (customers ?? []) as Customer[];
    if (oneTimers.length === 0) {
      return new Response(
        JSON.stringify({ candidates: 0, sent: 0, reason: "no_one_timers" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull their orders in the eligibility window (created 25-35 days ago).
    const { data: orderRows } = await supabase
      .from("orders")
      .select("customer_phone, customer_email, total, created_at")
      .gte("created_at", minFirstAt)
      .lte("created_at", maxFirstAt)
      .neq("source", "spin_game")
      .in("status", ["new", "confirmed", "shipped", "delivered", "paid"]);

    const phoneOrders = new Map<string, OrderRow>();
    const emailOrders = new Map<string, OrderRow>();
    for (const o of (orderRows ?? []) as OrderRow[]) {
      // Keep the most recent order per key (in case any anomaly).
      if (o.customer_phone) phoneOrders.set(o.customer_phone, o);
      if (o.customer_email) emailOrders.set(o.customer_email, o);
    }

    // 3. Pull cooldown set: customers who already got this nurture.
    const { data: prior } = await supabase
      .from("events")
      .select("metadata")
      .eq("event_type", "second_order_nurture_sent")
      .gte("created_at", new Date(now - 365 * dayMs).toISOString())
      .limit(5000);

    const alreadyNurtured = new Set<string>(
      (prior ?? [])
        .map((p) => {
          const md = p.metadata as Record<string, unknown> | null;
          return md && typeof md.customer_id === "string" ? md.customer_id : "";
        })
        .filter(Boolean),
    );

    // 4. Build eligible list.
    const eligible: Array<{ customer: Customer; firstOrderTotal: number; daysSince: number }> = [];
    for (const c of oneTimers) {
      if (alreadyNurtured.has(c.id)) continue;
      if (c.tags.some((t) => SKIP_TAGS.has(t))) continue;

      let order: OrderRow | undefined;
      if (c.phone) order = phoneOrders.get(c.phone);
      if (!order && c.email) order = emailOrders.get(c.email);
      if (!order) continue;

      const daysSince = Math.round((now - new Date(order.created_at).getTime()) / dayMs);
      eligible.push({ customer: c, firstOrderTotal: order.total, daysSince });
    }

    if (dryRun) {
      return new Response(
        JSON.stringify({
          candidates: eligible.length,
          dry_run: true,
          sample: eligible.slice(0, 10).map((e) => ({
            customer_id: e.customer.id,
            name: e.customer.name,
            first_order_total: e.firstOrderTotal,
            days_since: e.daysSince,
          })),
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 5. Send promo (cap to MAX_BATCH per run to spread API load).
    const batch = eligible.slice(0, MAX_BATCH);
    const expiresAt = new Date(now + PROMO_TTL_DAYS * dayMs).toISOString();
    const startsAt = new Date(now).toISOString();

    // Batch-mint all promo codes in a single insert.
    const promoRows = batch.map(() => ({
      code: generateCode(),
      discount_type: "percentage",
      discount_value: PROMO_DISCOUNT_PCT,
      max_uses: 1,
      min_order_amount: PROMO_MIN_ORDER,
      starts_at: startsAt,
      ends_at: expiresAt,
      is_active: true,
    }));
    const { data: promos, error: batchPromoErr } = await supabase
      .from("promo_codes")
      .insert(promoRows)
      .select("id, code");
    if (batchPromoErr || !promos || promos.length !== batch.length) {
      return new Response(
        JSON.stringify({ ok: false, error: batchPromoErr?.message ?? "promo batch insert mismatch" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Parallel Telegram sends.
    type SendResult =
      | { ok: true; customer: typeof batch[number]["customer"]; promo: { id: string; code: string }; firstOrderTotal: number; daysSince: number }
      | { ok: false; promoId: string };

    const sendResults: SendResult[] = await Promise.all(
      batch.map(async ({ customer, firstOrderTotal, daysSince }, i) => {
        const promo = promos[i];
        const firstName = (customer.name ?? "друже").split(" ")[0];
        const msg =
          `🐾 <b>${firstName}, ми скучили!</b>\n\n` +
          `Минуло вже ${daysSince} днів з вашого першого замовлення в BASIC.FOOD 💛\n` +
          `Хочемо повернути вас до улюбленого ласунчика — ось персональний подарунок:\n\n` +
          `🎫 <b>Промокод:</b>\n<code>${promo.code}</code>\n\n` +
          `💰 Знижка <b>−${PROMO_DISCOUNT_PCT}%</b> на наступне замовлення\n` +
          `🛒 Мінімум: ${PROMO_MIN_ORDER} ₴\n` +
          `⏰ Дійсний <b>${PROMO_TTL_DAYS} днів</b> (одноразовий)\n\n` +
          `🔗 <a href="https://basic-food.shop/catalog">Перейти до каталогу</a>`;

        const ok = await sendTelegram(Number(customer.telegram_chat_id), msg);
        if (!ok) return { ok: false, promoId: promo.id };
        return { ok: true, customer, promo, firstOrderTotal, daysSince };
      }),
    );

    // Deactivate orphaned promos (batch).
    const orphanIds = sendResults.filter((r): r is Extract<SendResult, { ok: false }> => !r.ok).map((r) => r.promoId);
    if (orphanIds.length > 0) {
      await supabase.from("promo_codes").update({ is_active: false }).in("id", orphanIds).catch(() => {});
    }

    // Batch insert success events.
    const sentItems = sendResults.filter((r): r is Extract<SendResult, { ok: true }> => r.ok);
    if (sentItems.length > 0) {
      await supabase.from("events").insert(
        sentItems.map(({ customer, promo, firstOrderTotal, daysSince }) => ({
          event_type: "second_order_nurture_sent",
          source: "acos_second_order_nurture",
          metadata: {
            customer_id: customer.id,
            chat_id: Number(customer.telegram_chat_id),
            promo_code: promo.code,
            promo_id: promo.id,
            first_order_total: firstOrderTotal,
            days_since: daysSince,
          },
        })),
      ).catch(() => {});
    }

    const sent = sentItems.length;
    const failed = orphanIds.length;
    const sentSamples = sentItems.slice(0, 5).map(({ customer, promo }) => ({ name: customer.name, code: promo.code }));

    // 6. Roll-up insight when we sent a meaningful batch.
    if (sent >= 3) {
      await supabase.from("ai_insights").insert({
        insight_type: "second_order_nurture_batch",
        title: `Надіслано NEXT15 промо ${sent} клієнтам з 1 замовленням`,
        description: `Автоматичний другий-замовлення nurture: ${sent} клієнтів отримали персональний промокод (15%, мін ${PROMO_MIN_ORDER}₴, ${PROMO_TTL_DAYS}д). Це клієнти, що зробили перше замовлення ${WINDOW_MIN_DAYS}-${WINDOW_MAX_DAYS} днів тому і не повернулися. Очікуваний redemption rate: 8-15%. Кандидатів у черзі: ${eligible.length - sent}.`,
        expected_impact: `Очікуваний return ${Math.round(sent * 0.12)} замовлень × ~${PROMO_MIN_ORDER}₴ = ~${Math.round(sent * 0.12 * PROMO_MIN_ORDER * 0.85).toLocaleString()}₴ виручки`,
        confidence: 0.6,
        risk_level: "low",
        affected_layer: "lifecycle",
        status: "new",
        metrics: {
          eligible_total: eligible.length,
          sent,
          failed,
          window_days: [WINDOW_MIN_DAYS, WINDOW_MAX_DAYS],
          discount_pct: PROMO_DISCOUNT_PCT,
          samples: sentSamples,
        },
      });
    }

    return new Response(
      JSON.stringify({
        candidates: eligible.length,
        sent,
        failed,
        skipped: oneTimers.length - eligible.length,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
