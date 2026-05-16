// ACOS Telegram Block Detector — runs daily via pg_cron.
// For every customer that was sent a promo touch in the last 30 days
// (winback_sent / cart_recovery_sent / auto_promo_sent) but never opened
// a chat back ("bot_started" / inbound message in the same window), we
// ping Telegram's getChat. If Telegram replies with "bot was blocked",
// we tag the customer `tg_blocked:<YYYY-MM-DD>` so future automation
// can skip them, and surface an insight summarizing the volume.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const TG_BOT_TOKEN = Deno.env.get("TELEGRAM_API_KEY") ?? Deno.env.get("TELEGRAM_API_KEY_1");
const LOOKBACK_DAYS = 30;
const MAX_CHECKS = 80; // guard against TG rate-limits

interface CheckResult {
  chat_id: number;
  blocked: boolean;
  reason?: string;
}

const checkChat = async (chatId: number): Promise<CheckResult> => {
  if (!TG_BOT_TOKEN) return { chat_id: chatId, blocked: false, reason: "no_token" };
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_BOT_TOKEN}/getChat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId }),
    });
    const json = await res.json().catch(() => ({}));
    if (json.ok) return { chat_id: chatId, blocked: false };
    const desc = String(json.description ?? "").toLowerCase();
    // Common Telegram errors that indicate the chat is unreachable.
    const isBlocked =
      desc.includes("blocked by the user") ||
      desc.includes("user is deactivated") ||
      desc.includes("chat not found") ||
      desc.includes("bot was kicked");
    return { chat_id: chatId, blocked: isBlocked, reason: desc };
  } catch (err) {
    return { chat_id: chatId, blocked: false, reason: (err as Error).message };
  }
};

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
    if (!TG_BOT_TOKEN) {
      return new Response(
        JSON.stringify({ error: "TELEGRAM_API_KEY missing" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const since = new Date(Date.now() - LOOKBACK_DAYS * 24 * 60 * 60 * 1000).toISOString();

    // 1. Find chat_ids that received automated touches in the window.
    const { data: sentEvents } = await supabase
      .from("events")
      .select("metadata")
      .in("event_type", ["winback_sent", "cart_recovery_sent", "auto_promo_sent"])
      .gte("created_at", since)
      .limit(2000);

    const candidateChatIds = new Set<number>();
    for (const e of sentEvents ?? []) {
      const meta = (e.metadata ?? {}) as Record<string, unknown>;
      const cid = Number(meta.chat_id);
      if (cid && !isNaN(cid)) candidateChatIds.add(cid);
    }

    if (candidateChatIds.size === 0) {
      return new Response(
        JSON.stringify({ candidates: 0, checked: 0, blocked: 0, reason: "no_recent_touches" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Drop chat_ids that have inbound messages in the window — clearly active.
    const { data: inbound } = await supabase
      .from("telegram_messages")
      .select("chat_id")
      .eq("direction", "in")
      .gte("created_at", since)
      .in("chat_id", Array.from(candidateChatIds));
    for (const m of inbound ?? []) {
      candidateChatIds.delete(Number(m.chat_id));
    }

    // 3. Drop chat_ids already tagged tg_blocked in customers (within 30d).
    const { data: customers } = await supabase
      .from("customers")
      .select("id, telegram_chat_id, tags")
      .in("telegram_chat_id", Array.from(candidateChatIds));

    const blockCutoff = Date.now() - 30 * 24 * 60 * 60 * 1000;
    const customerByChat = new Map<number, { id: string; tags: string[] }>();
    for (const c of customers ?? []) {
      if (!c.telegram_chat_id) continue;
      const tags = (c.tags ?? []) as string[];
      const existing = tags.find((t) => t.startsWith("tg_blocked:"));
      if (existing) {
        const ts = new Date(existing.split(":")[1]).getTime();
        if (!isNaN(ts) && ts >= blockCutoff) {
          candidateChatIds.delete(Number(c.telegram_chat_id));
          continue;
        }
      }
      customerByChat.set(Number(c.telegram_chat_id), { id: c.id, tags });
    }

    // 4. Probe TG for the remaining chat_ids (capped).
    const toCheck = Array.from(candidateChatIds).slice(0, MAX_CHECKS);
    const results: CheckResult[] = [];
    for (const cid of toCheck) {
      results.push(await checkChat(cid));
      // Tiny delay to avoid hammering TG.
      await new Promise((r) => setTimeout(r, 80));
    }

    // 5. Tag blocked customers + log insight.
    const blockedNow = results.filter((r) => r.blocked);
    const today = new Date().toISOString().slice(0, 10);

    const blockEventRows: object[] = [];
    await Promise.all(
      blockedNow.map(async (b) => {
        const cust = customerByChat.get(b.chat_id);
        if (!cust) return;
        const nextTags = [
          ...cust.tags.filter((t) => !t.startsWith("tg_blocked:")),
          `tg_blocked:${today}`,
        ];
        await supabase.from("customers").update({ tags: nextTags }).eq("id", cust.id);
        blockEventRows.push({
          event_type: "tg_block_detected",
          source: "acos",
          metadata: { chat_id: b.chat_id, reason: b.reason ?? null, customer_id: cust.id },
        });
      }),
    );
    if (blockEventRows.length > 0) {
      await supabase.from("events").insert(blockEventRows).catch(() => {});
    }

    if (blockedNow.length > 0) {
      const wastedCost = blockedNow.length * 5; // 5 UAH per touch wasted estimate
      await supabase.from("ai_insights").insert({
        insight_type: "tg_blocks_detected",
        title: `Telegram: знайдено ${blockedNow.length} заблокованих ботом клієнтів`,
        description: `Перевірено ${results.length} chat_id з нещодавніми промо-нагадуваннями. ${blockedNow.length} клієнтів заблокували бота — додано тег tg_blocked. Майбутні автоматизації їх пропустять.`,
        expected_impact: `Економія ~${wastedCost}₴/міс на марних спробах + чистіша база`,
        confidence: 0.92,
        risk_level: "low",
        affected_layer: "telegram_bot",
        status: "applied",
        metrics: {
          candidates: candidateChatIds.size,
          checked: results.length,
          blocked: blockedNow.length,
          chat_ids_blocked: blockedNow.map((b) => b.chat_id),
        },
      });
    }

    return new Response(
      JSON.stringify({
        candidates: candidateChatIds.size,
        checked: results.length,
        blocked: blockedNow.length,
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
