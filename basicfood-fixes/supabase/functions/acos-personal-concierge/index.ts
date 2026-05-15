// ACOS — Personal Concierge
// Autonomous per-customer re-engagement: for every eligible customer, the AI
// looks at their pet, last orders, favorite categories and writes a UNIQUE
// short message. Then routes it to the best channel:
//   1) Web Push  (instant, free, no-spammy)
//   2) Telegram  (linked chat)
//   3) Email     (last resort)
//
// Eligibility (default; per-user prefs override):
//   - Has at least 1 order
//   - Last order >= 14 days ago AND last concierge ping >= prefs.min_interval_days
//   - prefs.opted_out = false
//
// Cron: 1x/day at 11:00 Kyiv time. Processes up to BATCH_SIZE clients per run.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAIText } from "../_shared/ai-router.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

const BATCH_SIZE = 25;
const SITE_URL = "https://basic-food.shop";

interface CustomerCtx {
  user_id: string;
  email?: string | null;
  pet_name?: string | null;
  pet_type?: string | null;
  pet_breed?: string | null;
  last_order_at?: string | null;
  orders_count: number;
  favorite_categories: string[];
  recent_product_names: string[];
  recent_product_ids: string[];
  preferred_channel?: string | null;
}

async function buildCustomers(sb: any): Promise<CustomerCtx[]> {
  // Find users with: at least 1 order, last order >= 14d ago, no concierge ping in last 14d.
  const { data: rows, error } = await sb.rpc("personal_concierge_candidates", {
    p_min_days_since_order: 14,
    p_limit: BATCH_SIZE,
  }).maybeSingle();
  // Fallback if RPC missing: use raw query through views.
  if (error || !rows) {
    const { data: orders } = await sb
      .from("orders")
      .select("user_id, created_at")
      .not("user_id", "is", null)
      .order("created_at", { ascending: false })
      .limit(500);
    if (!orders?.length) return [];
    const lastByUser = new Map<string, string>();
    const countByUser = new Map<string, number>();
    for (const o of orders) {
      countByUser.set(o.user_id, (countByUser.get(o.user_id) ?? 0) + 1);
      if (!lastByUser.has(o.user_id)) lastByUser.set(o.user_id, o.created_at);
    }
    const cutoff = Date.now() - 14 * 86400000;
    const userIds = [...lastByUser.entries()]
      .filter(([_, d]) => new Date(d).getTime() < cutoff)
      .map(([u]) => u)
      .slice(0, BATCH_SIZE * 2);
    if (!userIds.length) return [];

    // Pull profiles, pets and prefs
    const [{ data: profs }, { data: prefs }] = await Promise.all([
      sb.from("profiles").select("id, email, full_name, pet_name, pet_type, pet_breed").in("id", userIds),
      sb.from("personal_concierge_prefs").select("user_id, opted_out, min_interval_days, preferred_channel").in("user_id", userIds),
    ]);

    const optedOut = new Set((prefs ?? []).filter((p: any) => p.opted_out).map((p: any) => p.user_id));

    // Throttle via last log
    const { data: lastLogs } = await sb
      .from("personal_concierge_log")
      .select("user_id, created_at")
      .in("user_id", userIds)
      .order("created_at", { ascending: false });
    const lastPing = new Map<string, string>();
    for (const r of lastLogs ?? []) if (!lastPing.has(r.user_id)) lastPing.set(r.user_id, r.created_at);

    // Pre-filter eligible user IDs without per-user queries.
    const preEligible: string[] = [];
    for (const uid of userIds) {
      if (optedOut.has(uid)) continue;
      const pref = (prefs ?? []).find((p: any) => p.user_id === uid);
      const minDays = pref?.min_interval_days ?? 14;
      const last = lastPing.get(uid);
      if (last && Date.now() - new Date(last).getTime() < minDays * 86400000) continue;
      preEligible.push(uid);
      if (preEligible.length >= BATCH_SIZE) break;
    }

    // Batch-fetch order IDs + items for all eligible users (replaces N per-user queries).
    const itemsByUser = new Map<string, { product_id: string; product_name: string }[]>();
    if (preEligible.length > 0) {
      const { data: eligibleOrders } = await sb
        .from("orders")
        .select("id, user_id")
        .in("user_id", preEligible);
      const allOrderIds = (eligibleOrders ?? []).map((o: any) => o.id as string);
      const orderUserMap = new Map<string, string>((eligibleOrders ?? []).map((o: any) => [o.id, o.user_id]));
      if (allOrderIds.length > 0) {
        const { data: allItems } = await sb
          .from("order_items")
          .select("product_id, product_name, order_id")
          .in("order_id", allOrderIds);
        for (const item of (allItems ?? []) as any[]) {
          const uid = orderUserMap.get(item.order_id);
          if (!uid) continue;
          const arr = itemsByUser.get(uid) ?? [];
          arr.push({ product_id: item.product_id, product_name: item.product_name });
          itemsByUser.set(uid, arr);
        }
      }
    }

    const eligible: CustomerCtx[] = [];
    for (const uid of preEligible) {
      const prof = (profs ?? []).find((p: any) => p.id === uid);
      const items = (itemsByUser.get(uid) ?? []).slice(0, 8);
      const recentNames = [...new Set(items.map((i) => i.product_name).filter(Boolean))].slice(0, 5);
      const recentIds = [...new Set(items.map((i) => i.product_id).filter(Boolean))].slice(0, 5);

      eligible.push({
        user_id: uid,
        email: prof?.email ?? null,
        pet_name: prof?.pet_name ?? null,
        pet_type: prof?.pet_type ?? null,
        pet_breed: prof?.pet_breed ?? null,
        last_order_at: lastByUser.get(uid) ?? null,
        orders_count: countByUser.get(uid) ?? 1,
        favorite_categories: [],
        recent_product_names: recentNames,
        recent_product_ids: recentIds,
        preferred_channel: (prefs ?? []).find((p: any) => p.user_id === uid)?.preferred_channel ?? null,
      });
    }
    return eligible;
  }
  return rows as CustomerCtx[];
}

async function generateMessage(ctx: CustomerCtx): Promise<{ text: string; reason: string }> {
  const pet = ctx.pet_name ? `улюбленець на ім'я ${ctx.pet_name}` : "улюбленець";
  const petKind = ctx.pet_type === "cat" ? "котик" : ctx.pet_type === "dog" ? "песик" : "тваринка";
  const lastOrderDays = ctx.last_order_at
    ? Math.round((Date.now() - new Date(ctx.last_order_at).getTime()) / 86400000)
    : null;
  const recents = ctx.recent_product_names.length
    ? `Минулого разу брали: ${ctx.recent_product_names.join(", ")}.`
    : "Раніше вже куштували наші ласощі.";

  const hasPetName = !!ctx.pet_name;
  const sys = `Ти — turbo-дружній асистент бренду BASIC.FOOD (натуральні в'ялені ласощі для тварин з яловичих субпродуктів).
Пишеш українською, тепло, без капсу, без хештегів, без емодзі більше 2 шт.
Максимум 220 символів. Без слова "знижка" якщо її немає в контексті.
Не вигадуй фактів про тварину — використовуй лише дані з контексту.
КРИТИЧНО: НІКОЛИ не вставляй плейсхолдери у квадратних/фігурних дужках типу [ім'я тварини], [name], {pet_name}, [порода] тощо. Якщо ім'я тварини не вказане в контексті — пиши просто "улюбленець" / "ваш песик" / "ваш котик" БЕЗ жодних дужок. Текст має йти одразу клієнту як є.`;

  const petLine = hasPetName
    ? `- Ім'я тварини: ${ctx.pet_name} (${petKind})`
    : `- Ім'я тварини: НЕ ВІДОМЕ — звертайся узагальнено ("ваш улюбленець"), без плейсхолдерів`;

  const usr = `Контекст клієнта:
${petLine}
- Кількість замовлень: ${ctx.orders_count}
- Останнє замовлення: ${lastOrderDays ?? "?"} днів тому
- ${recents}

Завдання: напиши КОРОТКЕ персональне повідомлення-нагадування. Без обіцянок акцій. Заклик подивитись каталог або повторити улюблене. ${hasPetName ? `Звернись до тварини на ім'я "${ctx.pet_name}".` : "Імені тварини НЕМАЄ — пиши узагальнено, БЕЗ дужок і плейсхолдерів."} ВИВІД: тільки текст, без префіксів.`;

  try {
    const text = await routeAIText({
      messages: [
        { role: "system", content: sys },
        { role: "user", content: usr },
      ],
      model: "google/gemini-2.5-flash",
      temperature: 0.85,
      max_tokens: 200,
      noCache: true,
    });
    let cleaned = (text || "").trim().slice(0, 240);
    cleaned = sanitizePlaceholders(cleaned, ctx.pet_name);
    if (!cleaned) throw new Error("empty");
    return { text: cleaned, reason: `re-engage; orders=${ctx.orders_count}; days_since=${lastOrderDays}` };
  } catch (_) {
    const fallback = ctx.pet_name
      ? `${ctx.pet_name} давно не куштував наші ласощі 🐾 Зазирніть у каталог — повторити улюблене легко: ${SITE_URL}/catalog`
      : `Ваш улюбленець скучив за натуральними ласощами 🐾 Зазирніть у каталог: ${SITE_URL}/catalog`;
    return { text: fallback, reason: "fallback" };
  }
}

/**
 * Видаляє AI-галюциновані плейсхолдери типу [ім'я тварини], {pet_name}, [name], [порода].
 * Якщо є реальне ім'я — підставляє; інакше замінює узагальненим "улюбленця/улюбленець".
 * Також прибирає порожні дужки що залишились та подвійні пробіли.
 */
function sanitizePlaceholders(text: string, petName: string | null | undefined): string {
  if (!text) return text;
  const nameRx = /\[\s*(ім.я\s+тварини|ім.я|name|pet[_\s]?name|кличка|твар(и|і)нка)\s*\]|\{\s*(pet[_\s]?name|name|ім.я)\s*\}/giu;
  const breedRx = /\[\s*(порода|breed)\s*\]|\{\s*(порода|breed)\s*\}/giu;
  const replacement = petName?.trim() ? petName.trim() : "вашого улюбленця";
  let out = text.replace(nameRx, replacement);
  out = out.replace(breedRx, "");
  // Прибрати залишкові порожні квадратні/фігурні дужки
  out = out.replace(/\[\s*\]|\{\s*\}/g, "");
  // Стиснути пробіли
  out = out.replace(/[ \t]{2,}/g, " ").replace(/\s+([,.!?])/g, "$1").trim();
  return out;
}

async function tryWebPush(sb: any, supabaseUrl: string, serviceKey: string, userId: string, text: string) {
  const { data: subs } = await sb
    .from("web_push_subscriptions")
    .select("id")
    .eq("user_id", userId)
    .eq("is_active", true)
    .limit(1);
  if (!subs?.length) return { ok: false, reason: "no_subscription" };
  try {
    const r = await fetch(`${supabaseUrl}/functions/v1/send-web-push`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${serviceKey}`,
        "x-cron-secret": Deno.env.get("CRON_SECRET") ?? "",
      },
      body: JSON.stringify({
        user_id: userId,
        title: "BASIC.FOOD",
        body: text.length > 110 ? text.slice(0, 107) + "…" : text,
        url: `${SITE_URL}/catalog`,
      }),
    });
    if (r.ok) return { ok: true };
    return { ok: false, reason: `push_${r.status}` };
  } catch (e) {
    return { ok: false, reason: `push_err:${(e as Error).message}` };
  }
}

async function tryTelegram(sb: any, userId: string, text: string): Promise<{ ok: boolean; reason?: string }> {
  const { data: row } = await sb
    .from("telegram_chat_ids")
    .select("chat_id")
    .eq("user_id", userId)
    .maybeSingle();
  if (!row?.chat_id) return { ok: false, reason: "no_tg_chat" };
  const token = Deno.env.get("TELEGRAM_API_KEY");
  if (!token) return { ok: false, reason: "no_tg_token" };
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: row.chat_id,
        text: `${text}\n\n${SITE_URL}/catalog`,
        disable_web_page_preview: false,
      }),
    });
    if (r.ok) return { ok: true };
    return { ok: false, reason: `tg_${r.status}` };
  } catch (e) {
    return { ok: false, reason: `tg_err:${(e as Error).message}` };
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const sb = createClient(supabaseUrl, serviceKey);

    const customers = await buildCustomers(sb);
    let processed = 0, delivered = 0, skipped = 0;
    const channelStats: Record<string, number> = {};

    for (const ctx of customers) {
      processed++;
      const { text, reason } = await generateMessage(ctx);

      // Channel preference: already fetched in buildCustomers, no extra query needed
      const pref = ctx.preferred_channel ?? "auto";

      let channel: "web_push" | "telegram" | "email" | "none" = "none";
      let deliverError: string | null = null;
      let deliveredOk = false;

      const order = pref === "auto" ? ["web_push", "telegram"] : [pref];
      for (const ch of order) {
        if (ch === "web_push") {
          const r = await tryWebPush(sb, supabaseUrl, serviceKey, ctx.user_id, text);
          if (r.ok) { channel = "web_push"; deliveredOk = true; break; }
          deliverError = r.reason ?? null;
        } else if (ch === "telegram") {
          const r = await tryTelegram(sb, ctx.user_id, text);
          if (r.ok) { channel = "telegram"; deliveredOk = true; break; }
          deliverError = r.reason ?? null;
        }
      }

      if (deliveredOk) {
        delivered++;
        channelStats[channel] = (channelStats[channel] ?? 0) + 1;
      } else {
        skipped++;
      }

      await sb.from("personal_concierge_log").insert({
        user_id: ctx.user_id,
        channel,
        message_text: text,
        reason,
        pet_context: {
          pet_name: ctx.pet_name,
          pet_type: ctx.pet_type,
          pet_breed: ctx.pet_breed,
        },
        product_ids: ctx.recent_product_ids,
        delivered: deliveredOk,
        delivery_error: deliveredOk ? null : deliverError,
      });
    }

    return new Response(
      JSON.stringify({ ok: true, processed, delivered, skipped, channels: channelStats }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ ok: false, error: (e as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
