// acos-bot-reorder-reminder
//
// Telegram-channel parallel of acos-reorder-reminder-engine.
// Targets users WITHOUT active device tokens but WITH a telegram_chat_id
// (registered via the bot). Closes the retention loop for ~70% of the
// customer base who never installed the APK.
//
// Logic:
//   1. Find delivered/completed orders 14d old (±1d), no follow-up order
//   2. Resolve customer by phone/email → check telegram_chat_id presence
//   3. Skip if reminder already sent (any channel) OR user has device tokens
//      (push channel will handle them — no double-touch)
//   4. Send via TELEGRAM_API_KEY using sendMessage with deep-link button
//      to https://basic-food.shop/reorder/<order_id>
//   5. Log bot_reorder_reminder_sent for ACOS attribution
//
// Idempotency: events table dedup by order_id + event_type.
//
// Why complementary to push (not replacement):
//   - Mobile-web users (no APK) → bot is the ONLY warm channel
//   - APK users get push (richer UX, instant tap-through)
//   - Both events flow into the same /reorder/:id deep-link

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const SITE_URL = "https://basic-food.shop";

interface CandidateOrder {
  id: string;
  user_id: string | null;
  customer_name: string | null;
  customer_phone: string | null;
  customer_email: string | null;
  total: number;
  created_at: string;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-bot-reorder-reminder", req, null, async () => {
    const __res = await (async () => {

  const url = new URL(req.url);
  const reminderDays = parseInt(url.searchParams.get("days") ?? "14", 10);
  const dryRun = url.searchParams.get("dry_run") === "1";

  const botToken = Deno.env.get("TELEGRAM_API_KEY");
  if (!botToken && !dryRun) {
    return new Response(
      JSON.stringify({ ok: false, error: "TELEGRAM_API_KEY missing" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const windowStart = new Date(Date.now() - (reminderDays + 1) * 86400_000);
  const windowEnd = new Date(Date.now() - (reminderDays - 1) * 86400_000);

  const { data: candidates, error } = await supabase
    .from("orders")
    .select("id, user_id, customer_name, customer_phone, customer_email, total, created_at")
    .in("status", ["delivered", "completed", "paid", "shipped"])
    .gte("created_at", windowStart.toISOString())
    .lt("created_at", windowEnd.toISOString())
    .returns<CandidateOrder[]>();

  if (error) {
    console.error("[bot-reorder-reminder] fetch failed", error);
    return new Response(JSON.stringify({ ok: false, error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (!candidates?.length) {
    return new Response(
      JSON.stringify({ ok: true, candidates: 0, sent: 0, skipped: 0 }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // ---- BATCH PREFETCH (replaces N+1 per-order DB queries) ----
  const orderIds = candidates.map((c) => c.id);
  const userIds = [...new Set(candidates.filter((c) => c.user_id).map((c) => c.user_id as string))];
  const allPhones = [...new Set(candidates.filter((c) => c.customer_phone).map((c) => c.customer_phone as string))];
  const emailsOnly = [...new Set(candidates.filter((c) => !c.customer_phone && c.customer_email).map((c) => c.customer_email as string))];

  const [recentUserOrdersRes, recentPhoneOrdersRes, remindersRes, tokensRes, custByPhoneRes, custByEmailRes] =
    await Promise.all([
      userIds.length > 0
        ? supabase.from("orders").select("user_id, created_at").in("user_id", userIds).gt("created_at", windowStart.toISOString())
        : Promise.resolve({ data: [] as { user_id: string; created_at: string }[] }),
      allPhones.length > 0
        ? supabase.from("orders").select("customer_phone, created_at").in("customer_phone", allPhones).gt("created_at", windowStart.toISOString())
        : Promise.resolve({ data: [] as { customer_phone: string; created_at: string }[] }),
      supabase.from("events").select("order_id").in("event_type", ["reorder_reminder_sent", "bot_reorder_reminder_sent"]).in("order_id", orderIds),
      userIds.length > 0
        ? supabase.from("device_tokens").select("user_id").in("user_id", userIds).eq("is_active", true)
        : Promise.resolve({ data: [] as { user_id: string }[] }),
      allPhones.length > 0
        ? supabase.from("customers").select("phone, telegram_chat_id").in("phone", allPhones).not("telegram_chat_id", "is", null)
        : Promise.resolve({ data: [] as { phone: string; telegram_chat_id: number }[] }),
      emailsOnly.length > 0
        ? supabase.from("customers").select("email, telegram_chat_id").in("email", emailsOnly).not("telegram_chat_id", "is", null)
        : Promise.resolve({ data: [] as { email: string; telegram_chat_id: number }[] }),
    ]);

  // Build lookup maps
  const recentByUser = new Map<string, string[]>();
  for (const o of (recentUserOrdersRes.data ?? []) as { user_id: string; created_at: string }[]) {
    const arr = recentByUser.get(o.user_id) ?? [];
    arr.push(o.created_at);
    recentByUser.set(o.user_id, arr);
  }
  const recentByPhone = new Map<string, string[]>();
  for (const o of (recentPhoneOrdersRes.data ?? []) as { customer_phone: string; created_at: string }[]) {
    const arr = recentByPhone.get(o.customer_phone) ?? [];
    arr.push(o.created_at);
    recentByPhone.set(o.customer_phone, arr);
  }
  const alreadyReminded = new Set((remindersRes.data ?? []).map((e: any) => e.order_id as string));
  const usersWithPush = new Set((tokensRes.data ?? []).map((t: any) => t.user_id as string));
  const chatIdByPhone = new Map<string, number>();
  for (const c of (custByPhoneRes.data ?? []) as any[]) {
    if (c.phone && c.telegram_chat_id != null) chatIdByPhone.set(c.phone, Number(c.telegram_chat_id));
  }
  const chatIdByEmail = new Map<string, number>();
  for (const c of (custByEmailRes.data ?? []) as any[]) {
    if (c.email && c.telegram_chat_id != null) chatIdByEmail.set(c.email, Number(c.telegram_chat_id));
  }

  // ---- IN-MEMORY FILTER ----
  let sent = 0;
  let skipped = 0;
  const skipReasons: Record<string, number> = {};
  const bump = (k: string) => (skipReasons[k] = (skipReasons[k] ?? 0) + 1);
  const toSend: { order: CandidateOrder; chatId: number }[] = [];

  for (const order of candidates) {
    // 1. Require phone or user_id to check for reorders
    if (!order.user_id && !order.customer_phone) { skipped++; bump("no_identity"); continue; }

    // 2. Skip if user already reordered
    const recentDates = order.user_id
      ? (recentByUser.get(order.user_id) ?? [])
      : (recentByPhone.get(order.customer_phone!) ?? []);
    if (recentDates.some((t) => t > order.created_at)) { skipped++; bump("already_reordered"); continue; }

    // 3. Skip if any reminder already sent for this order
    if (alreadyReminded.has(order.id)) { skipped++; bump("already_reminded"); continue; }

    // 4. Skip if user has active device tokens — push engine handles them
    if (order.user_id && usersWithPush.has(order.user_id)) { skipped++; bump("has_push_channel"); continue; }

    // 5. Resolve telegram_chat_id
    let chatId: number | null = null;
    if (order.customer_phone) chatId = chatIdByPhone.get(order.customer_phone) ?? null;
    else if (order.customer_email) chatId = chatIdByEmail.get(order.customer_email) ?? null;
    if (!chatId) { skipped++; bump("no_telegram_chat"); continue; }

    if (dryRun) { sent++; continue; }
    toSend.push({ order, chatId });
  }

  // ---- PARALLEL SEND ----
  const sendResults = await Promise.all(toSend.map(async ({ order, chatId }) => {
    const firstName = (order.customer_name ?? "").split(" ")[0] || "Друже";
    const text =
      `🐾 *${firstName}*, час поповнити запас!\n\n` +
      `Минуло ~${reminderDays} днів з вашого замовлення — улюбленець, мабуть, доїдає попередні ласощі.\n\n` +
      `Один тап — і повторюємо те саме замовлення з актуальними цінами.`;
    const replyMarkup = {
      inline_keyboard: [[
        { text: "🔁 Повторити замовлення", url: `${SITE_URL}/reorder/${order.id}` },
        { text: "🛒 До каталогу", url: `${SITE_URL}/catalog` },
      ]],
    };
    try {
      const tgRes = await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text, parse_mode: "Markdown", reply_markup: replyMarkup }),
      });
      if (tgRes.ok) return { ok: true, order, chatId };
      const errText = await tgRes.text();
      console.warn("[bot-reorder-reminder] tg send failed", order.id, errText.slice(0, 200));
      return { ok: false, order, chatId };
    } catch (e) {
      console.warn("[bot-reorder-reminder] exception", order.id, e);
      return { ok: false, order, chatId };
    }
  }));

  // ---- BATCH EVENT INSERT ----
  const eventRows: object[] = [];
  for (const r of sendResults) {
    if (r.ok) {
      sent++;
      eventRows.push({
        event_type: "bot_reorder_reminder_sent",
        source: "acos-bot-reorder-reminder",
        user_id: r.order.user_id,
        order_id: r.order.id,
        metadata: {
          campaign: "bot_reorder_reminder_d14",
          reminder_days: reminderDays,
          channel: "telegram",
          chat_id: r.chatId,
        },
      });
    } else {
      skipped++; bump("tg_send_failed");
    }
  }
  if (eventRows.length > 0) {
    await supabase.from("events").insert(eventRows);
  }

  return new Response(
    JSON.stringify({
      ok: true,
      candidates: candidates.length,
      sent,
      skipped,
      skip_reasons: skipReasons,
      window: { start: windowStart, end: windowEnd, days: reminderDays },
      dry_run: dryRun,
    }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
    })();
    return { response: __res };
  });
});
