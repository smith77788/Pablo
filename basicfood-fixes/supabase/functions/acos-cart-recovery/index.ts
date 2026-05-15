// ACOS Cart Abandonment Recovery — runs every 30 min via pg_cron.
// Identifies users who triggered add_to_cart >30min ago but never reached
// purchase_completed in this session. If they have a linked telegram_chat_id,
// sends a personalized recovery message with a 10% single-use promo code.
// Cooldown: 7 days per user (tracked via cart_recovery_sent events).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const TG_BOT_TOKEN = Deno.env.get("TELEGRAM_API_KEY") ?? Deno.env.get("TELEGRAM_API_KEY_1");

interface AbandonedCart {
  user_id: string;
  session_id: string;
  last_add_at: string;
  product_id: string | null;
  chat_id: number;
}

const generateCode = (): string => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let s = "CART10-";
  for (let i = 0; i < 6; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
};

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

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-cart-recovery", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const since30 = new Date(Date.now() - 30 * 60 * 1000).toISOString();
    const since4h = new Date(Date.now() - 4 * 60 * 60 * 1000).toISOString();
    const cooldownSince = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();

    // 1. Get add_to_cart events from authenticated users in last 4h, older than 30min.
    const { data: cartEvents } = await supabase
      .from("events")
      .select("user_id, session_id, product_id, created_at")
      .eq("event_type", "add_to_cart")
      .not("user_id", "is", null)
      .gte("created_at", since4h)
      .lte("created_at", since30)
      .order("created_at", { ascending: false })
      .limit(200);

    if (!cartEvents || cartEvents.length === 0) {
      return new Response(JSON.stringify({ candidates: 0, sent: 0, reason: "no_carts" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Dedupe by user_id (keep most recent add_to_cart per user)
    const userCartMap = new Map<string, AbandonedCart>();
    for (const e of cartEvents) {
      if (!e.user_id) continue;
      if (!userCartMap.has(e.user_id)) {
        userCartMap.set(e.user_id, {
          user_id: e.user_id,
          session_id: e.session_id ?? "",
          last_add_at: e.created_at,
          product_id: e.product_id,
          chat_id: 0,
        });
      }
    }

    const userIds = Array.from(userCartMap.keys());

    // 2. Exclude users who completed purchase in this window
    const { data: purchases } = await supabase
      .from("events")
      .select("user_id")
      .eq("event_type", "purchase_completed")
      .in("user_id", userIds)
      .gte("created_at", since4h);
    for (const p of purchases ?? []) {
      if (p.user_id) userCartMap.delete(p.user_id);
    }

    // 3. Exclude users in cooldown (sent within last 7 days)
    const { data: recentSends } = await supabase
      .from("events")
      .select("user_id")
      .eq("event_type", "cart_recovery_sent")
      .in("user_id", Array.from(userCartMap.keys()))
      .gte("created_at", cooldownSince);
    for (const s of recentSends ?? []) {
      if (s.user_id) userCartMap.delete(s.user_id);
    }

    // 4. Get telegram_chat_id for remaining users
    const remainingUserIds = Array.from(userCartMap.keys());
    if (remainingUserIds.length === 0) {
      return new Response(JSON.stringify({ candidates: 0, sent: 0, reason: "all_filtered" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const { data: tgLinks } = await supabase
      .from("telegram_chat_ids")
      .select("user_id, chat_id")
      .in("user_id", remainingUserIds);

    const tgMap = new Map<string, number>();
    for (const t of tgLinks ?? []) {
      if (t.user_id && t.chat_id) tgMap.set(t.user_id, Number(t.chat_id));
    }

    // Skip users whose linked customer has active promo_paused (14d) or tg_blocked (30d) tag.
    // Match customers by telegram_chat_id since cart recovery delivers via TG.
    const PAUSE_DAYS = 14;
    const BLOCK_DAYS = 30;
    const pauseCutoff = Date.now() - PAUSE_DAYS * 24 * 60 * 60 * 1000;
    const blockCutoff = Date.now() - BLOCK_DAYS * 24 * 60 * 60 * 1000;
    const hasFreshTag = (tags: string[] | null | undefined, prefix: string, cutoff: number) => {
      if (!tags?.length) return false;
      const tag = tags.find((t) => t.startsWith(prefix));
      if (!tag) return false;
      const ts = new Date(tag.split(":")[1]).getTime();
      return !isNaN(ts) && ts >= cutoff;
    };
    const chatIds = Array.from(tgMap.values());
    if (chatIds.length > 0) {
      const { data: skipCustomers } = await supabase
        .from("customers")
        .select("telegram_chat_id, tags")
        .in("telegram_chat_id", chatIds);
      const skipChatIds = new Set<number>();
      for (const c of skipCustomers ?? []) {
        if (!c.telegram_chat_id) continue;
        const tags = c.tags as string[] | null;
        if (
          hasFreshTag(tags, "promo_paused:", pauseCutoff) ||
          hasFreshTag(tags, "tg_blocked:", blockCutoff)
        ) {
          skipChatIds.add(Number(c.telegram_chat_id));
        }
      }
      // Drop skipped users from tgMap (so the send loop will skip them via the `if (!chatId)` check)
      for (const [userId, chatId] of tgMap.entries()) {
        if (skipChatIds.has(chatId)) tgMap.delete(userId);
      }
    }

    // 5. Send recovery message + create promo code for each

    // Batch-fetch all product names in one query (avoids N+1)
    const allProductIds = [...new Set(
      [...userCartMap.values()].map(c => c.product_id).filter(Boolean)
    )] as string[];
    const productNameMap = new Map<string, string>();
    if (allProductIds.length > 0) {
      const { data: products } = await supabase
        .from("products")
        .select("id, name")
        .in("id", allProductIds);
      for (const p of products ?? []) productNameMap.set(p.id, p.name);
    }

    let sent = 0;
    let orphanedCodes = 0;
    const expiresAt = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();

    for (const [userId, cart] of userCartMap.entries()) {
      const chatId = tgMap.get(userId);
      if (!chatId) continue;

      const productName = (cart.product_id && productNameMap.get(cart.product_id)) || "ваші ласощі";

      // Create single-use promo code
      const code = generateCode();
      const { error: pcErr } = await supabase.from("promo_codes").insert({
        code,
        discount_type: "percent",
        discount_value: 10,
        max_uses: 1,
        min_order_amount: 200,
        is_active: true,
        ends_at: expiresAt,
      });
      if (pcErr) {
        console.error("[cart-recovery] promo insert failed:", pcErr.message, "user:", userId);
        continue;
      }

      const text =
        `🛒 <b>Ваш кошик чекає!</b>\n\n` +
        `Ви залишили <b>${productName}</b> у кошику.\n\n` +
        `Тримайте знижку <b>-10%</b> на завершення замовлення:\n` +
        `<code>${code}</code>\n\n` +
        `⏰ Промокод діє <b>24 години</b>.\n` +
        `🛍 Мін. сума: 200₴\n\n` +
        `<a href="https://basic-food.shop/checkout">Завершити замовлення →</a>`;

      const ok = await sendTelegram(chatId, text);
      if (!ok) {
        // Promo code created but message not sent — track orphan
        orphanedCodes++;
        console.error("[cart-recovery] telegram failed for chat_id:", chatId, "promo:", code);
        await supabase.from("events").insert({
          event_type: "cart_recovery_failed",
          user_id: userId,
          product_id: cart.product_id,
          source: "acos",
          metadata: { promo_code: code, chat_id: chatId, reason: "telegram_send_failed" },
        }).catch(() => {});
        continue;
      }

      // Log cooldown event
      await supabase.from("events").insert({
        event_type: "cart_recovery_sent",
        user_id: userId,
        product_id: cart.product_id,
        source: "acos",
        metadata: { promo_code: code, chat_id: chatId },
      });
      sent++;
    }

    // 6. Log insight summary
    if (sent > 0) {
      await supabase.from("ai_insights").insert({
        insight_type: "cart_recovery_campaign",
        title: `Cart recovery: надіслано ${sent} нагадувань`,
        description: `Знайдено ${userCartMap.size} покинутих кошиків з прив'язаним Telegram. Надіслано ${sent} персональних промокодів CART10 (-10%, 24г, мін. 200₴).`,
        expected_impact: `+${Math.round(sent * 0.12)} recovery замовлень (~12% conversion)`,
        confidence: 0.78,
        risk_level: "low",
        affected_layer: "telegram_bot",
        status: "applied",
        metrics: { candidates: userCartMap.size, sent, orphaned_codes: orphanedCodes, has_telegram: tgMap.size },
      });
    }

    return new Response(
      JSON.stringify({
        candidates: userCartMap.size,
        with_telegram: tgMap.size,
        sent,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
    })();
    return { response: __res };
  });
});
