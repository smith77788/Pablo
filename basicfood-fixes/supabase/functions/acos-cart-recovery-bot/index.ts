// ACOS Smart Cart Recovery via Telegram
//
// Logic:
//   1. Find sessions with `add_to_cart` events in last 4h that did NOT lead
//      to `purchase_completed`.
//   2. Resolve to a customer with telegram_chat_id (if user_id is known
//      via events.user_id → profiles.telegram_chat_id, or session_id ↔ chat_id
//      from earlier bot interactions).
//   3. Skip if a recovery attempt was already sent in last 24h.
//   4. Send personalised Telegram DM with up to 3 abandoned products and
//      a small promo nudge ("забув чи передумав?" + 5% promo код RECOVER5).
//
// Conversion tracking: when an order is created within 48h after a sent
// attempt, `acos-cart-recovery-tracker` (cron) will mark recovered_at +
// recovered_value. This file just sends.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};
const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-cart-recovery-bot", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const sinceISO = new Date(Date.now() - 4 * 3600_000).toISOString();
    const cutoffNoBuyISO = new Date(Date.now() - 30 * 60_000).toISOString();

    // ── Pull recent ATC events ──
    const { data: atc } = await supabase
      .from("events")
      .select("session_id, user_id, product_id, created_at, metadata")
      .eq("event_type", "add_to_cart")
      .gte("created_at", sinceISO)
      .lt("created_at", cutoffNoBuyISO)
      .not("session_id", "is", null);

    if (!atc?.length) {
      return json({ ok: true, action: "noop", reason: "no_atc_in_window" });
    }

    // Group by session
    const bySession = new Map<string, { user_id: string | null; products: Set<string>; ts: string }>();
    for (const e of atc) {
      const sid = e.session_id as string;
      const cur = bySession.get(sid) ?? { user_id: e.user_id ?? null, products: new Set(), ts: e.created_at };
      if (e.product_id) cur.products.add(e.product_id);
      cur.user_id = cur.user_id ?? e.user_id ?? null;
      cur.ts = e.created_at;
      bySession.set(sid, cur);
    }

    // Filter out sessions that already purchased
    const sessionIds = [...bySession.keys()];
    const { data: purchases } = await supabase
      .from("events")
      .select("session_id")
      .eq("event_type", "purchase_completed")
      .in("session_id", sessionIds)
      .gte("created_at", sinceISO);
    const purchased = new Set((purchases ?? []).map((p) => p.session_id));

    const candidates = [...bySession.entries()].filter(([sid]) => !purchased.has(sid));

    // Skip sessions that already got an attempt in last 24h
    const recentAttemptSince = new Date(Date.now() - 24 * 3600_000).toISOString();
    const candidateSessionIds = candidates.map(([sid]) => sid);
    const { data: recentAttempts } = candidateSessionIds.length
      ? await supabase.from("cart_recovery_attempts")
          .select("session_id").in("session_id", candidateSessionIds)
          .gte("created_at", recentAttemptSince)
      : { data: [] as { session_id: string }[] };
    const alreadySent = new Set((recentAttempts ?? []).map((r) => r.session_id));

    // Resolve user → chat_id
    const userIds = [...new Set(candidates.map(([, v]) => v.user_id).filter(Boolean))] as string[];
    const userToChat = new Map<string, number>();
    if (userIds.length) {
      // INC-0001 hotfix: telegram_chat_id lives in telegram_chat_ids, not profiles
      const { data: profs } = await supabase
        .from("telegram_chat_ids").select("user_id, chat_id")
        .in("user_id", userIds).not("chat_id", "is", null);
      for (const p of profs ?? []) {
        if (p.chat_id) userToChat.set(p.user_id, Number(p.chat_id));
      }
    }

    // Resolve products
    const productIds = [...new Set(candidates.flatMap(([, v]) => [...v.products]))];
    const { data: products } = productIds.length
      ? await supabase.from("products").select("id, name, price, image_url").in("id", productIds)
      : { data: [] as any[] };
    const productMap = new Map((products ?? []).map((p: any) => [p.id, p]));

    const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
    const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY");

    const sent: any[] = [];
    const skipped: any[] = [];

    // Pre-filter candidates in-memory, then parallel send
    type RecoveryItem = { sid: string; chatId: number; items: any[]; cartValue: number; lines: string };
    const toSend: RecoveryItem[] = [];
    for (const [sid, info] of candidates) {
      if (alreadySent.has(sid)) { skipped.push({ sid, reason: "recent_attempt" }); continue; }
      const chatId = info.user_id ? userToChat.get(info.user_id) : undefined;
      if (!chatId) { skipped.push({ sid, reason: "no_chat_id" }); continue; }
      const items = [...info.products].map((pid) => productMap.get(pid)).filter(Boolean);
      if (!items.length) { skipped.push({ sid, reason: "no_resolved_products" }); continue; }
      const cartValue = items.reduce((s: number, it: any) => s + (it?.price ?? 0), 0);
      const lines = [
        `👋 <b>Не дозабули нас?</b>`,
        ``,
        `У кошику чекає:`,
        ...items.slice(0, 3).map((it: any) => `• ${escapeHtml(it.name)} — ${it.price}₴`),
        ``,
        `🎁 Промокод <b>RECOVER5</b> — мінус 5% якщо завершите сьогодні.`,
        ``,
        `<a href="https://basic-food.shop/checkout">Перейти до оформлення →</a>`,
      ].join("\n");
      toSend.push({ sid, chatId, items, cartValue, lines });
    }

    // Parallel sends
    const sendResults = await Promise.all(
      toSend.map(async ({ sid, chatId, items, cartValue, lines }) => {
        let ok = false;
        if (LOVABLE_API_KEY && TELEGRAM_API_KEY) {
          try {
            const r = await fetch(`${GATEWAY_URL}/sendMessage`, {
              method: "POST",
              headers: {
                Authorization: `Bearer ${LOVABLE_API_KEY}`,
                "X-Connection-Api-Key": TELEGRAM_API_KEY,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({ chat_id: chatId, text: lines, parse_mode: "HTML", disable_web_page_preview: true }),
            });
            ok = r.ok;
          } catch (e) {
            console.error("[cart-recovery] send failed", chatId, e);
          }
        }
        return { sid, chatId, items, cartValue, lines, ok };
      }),
    );

    // Batch insert successful attempts
    const successItems = sendResults.filter((r) => r.ok);
    if (successItems.length > 0) {
      await supabase.from("cart_recovery_attempts").insert(
        successItems.map(({ chatId, sid, items, cartValue, lines }) => ({
          chat_id: chatId,
          session_id: sid,
          product_ids: items.map((it: any) => it.id),
          cart_value: cartValue,
          channel: "telegram",
          promo_code: "RECOVER5",
          message_text: lines,
          status: "sent",
        })),
      ).catch(() => {});
      for (const r of successItems) {
        sent.push({ sid: r.sid, chat_id: r.chatId, items: r.items.length, cart_value: r.cartValue });
      }
    }
    for (const r of sendResults.filter((r) => !r.ok)) {
      skipped.push({ sid: r.sid, reason: "send_failed_or_no_creds" });
    }

    return json({ ok: true, sent_count: sent.length, skipped_count: skipped.length, sent, skipped: skipped.slice(0, 10) });
  } catch (e) {
    console.error("[cart-recovery] fatal", e);
    return new Response(JSON.stringify({ error: String((e as Error)?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});

const json = (payload: unknown, status = 200) =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
