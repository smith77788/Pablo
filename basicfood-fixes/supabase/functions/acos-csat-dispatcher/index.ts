// ACOS CSAT Dispatcher
//
// Runs every 30 min. For each delivered order older than 48h that has no CSAT survey:
//   1) creates csat_surveys row (status=pending)
//   2) sends Telegram message asking 1-5 stars + optional comment
//   3) marks status=sent on success
//
// Inline keyboard: ⭐️ 1..5 → submits via callback_query (handled by telegram-poll → submit_csat_response RPC).
// Public-facing review link is included as fallback for non-Telegram customers.
//
// No-PII summary; safe to call on cron.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};
const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";
const SITE_URL = Deno.env.get("PUBLIC_SITE_URL") ?? "https://basic-food.shop";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-csat-dispatcher", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const tgKey = Deno.env.get("TELEGRAM_API_KEY_1") ?? Deno.env.get("TELEGRAM_API_KEY");

  try {
    const cutoff = new Date(Date.now() - 48 * 3600_000).toISOString();

    // Eligible: delivered ≥ 48h ago, not seed, no existing survey
    const { data: orders, error } = await supabase
      .from("orders")
      .select("id, customer_name, customer_phone, customer_email, total, updated_at")
      .eq("status", "delivered")
      .neq("source", "spin_game")
      .or("message.is.null,message.neq.seed")
      .lte("updated_at", cutoff)
      .gte("updated_at", new Date(Date.now() - 14 * 86400_000).toISOString())
      .limit(40);

    if (error) throw error;

    let created = 0;
    let sent = 0;
    let skipped = 0;

    const orderList = orders ?? [];
    if (orderList.length > 0) {
      const orderIds = orderList.map((o: any) => o.id as string);
      const phones = [...new Set(orderList.filter((o: any) => o.customer_phone).map((o: any) => o.customer_phone as string))];
      const emails = [...new Set(orderList.filter((o: any) => o.customer_email).map((o: any) => o.customer_email as string))];

      // Batch: dedup check + customer lookup in parallel
      const [existingSurveysRes, custByPhoneRes, custByEmailRes] = await Promise.all([
        supabase.from("csat_surveys").select("order_id").in("order_id", orderIds),
        phones.length > 0
          ? supabase.from("customers").select("id, phone, telegram_chat_id").in("phone", phones)
          : Promise.resolve({ data: [] as any[] }),
        emails.length > 0
          ? supabase.from("customers").select("id, email, telegram_chat_id").in("email", emails)
          : Promise.resolve({ data: [] as any[] }),
      ]);
      const surveyedOrders = new Set((existingSurveysRes.data ?? []).map((s: any) => s.order_id as string));
      const custByPhone = new Map((custByPhoneRes.data ?? []).map((c: any) => [c.phone as string, c]));
      const custByEmail = new Map((custByEmailRes.data ?? []).map((c: any) => [c.email as string, c]));

      const toProcess = orderList.filter((o: any) => {
        if (surveyedOrders.has(o.id as string)) { skipped++; return false; }
        return true;
      });

      const expiresAt = new Date(Date.now() + 14 * 86400_000).toISOString();

      // Parallel per-order: insert survey + send Telegram (survey ID needed in keyboard)
      const processResults = await Promise.all(
        toProcess.map(async (o: any) => {
          const cust = (o.customer_phone && custByPhone.get(o.customer_phone))
            || (o.customer_email && custByEmail.get(o.customer_email))
            || null;
          const customerId: string | null = cust?.id ?? null;
          const chatId: number | null = cust?.telegram_chat_id ?? null;

          const { data: surveyRow, error: insErr } = await supabase
            .from("csat_surveys")
            .insert({
              order_id: o.id,
              customer_id: customerId,
              channel: chatId ? "telegram" : "link",
              chat_id: chatId,
              status: "pending",
              expires_at: expiresAt,
            })
            .select("id")
            .single();

          if (insErr || !surveyRow) {
            console.error("[csat-dispatcher] insert failed", insErr);
            return { created: false, sent: false };
          }

          // Send Telegram if possible
          if (chatId && tgKey) {
            const orderShort = (o.id as string).slice(0, 8);
            const text =
              `⭐️ <b>Як вам ваше замовлення #${orderShort}?</b>\n\n` +
              `Привіт, ${((o.customer_name ?? "друже") as string).split(" ")[0]}!\n` +
              `Дякуємо за покупку в Basic Food. Оцініть досвід — нам важлива ваша думка 💛\n\n` +
              `<i>Ваша оцінка допоможе іншим тваринкам отримати найкраще!</i>`;
            const reply_markup = {
              inline_keyboard: [
                [
                  { text: "⭐️", callback_data: `csat:${surveyRow.id}:1` },
                  { text: "⭐️⭐️", callback_data: `csat:${surveyRow.id}:2` },
                  { text: "⭐️⭐️⭐️", callback_data: `csat:${surveyRow.id}:3` },
                ],
                [
                  { text: "⭐️⭐️⭐️⭐️", callback_data: `csat:${surveyRow.id}:4` },
                  { text: "⭐️⭐️⭐️⭐️⭐️", callback_data: `csat:${surveyRow.id}:5` },
                ],
                [{ text: "📝 Залишити відгук на сайті", url: `${SITE_URL}/reviews?survey=${surveyRow.id}` }],
              ],
            };
            try {
              const res = await fetch(`${GATEWAY_URL}/sendMessage`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${tgKey}` },
                body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", reply_markup, disable_web_page_preview: true }),
              });
              const status = res.ok ? "sent" : "failed";
              await supabase.from("csat_surveys").update(
                res.ok ? { status: "sent", sent_at: new Date().toISOString() } : { status: "failed" },
              ).eq("id", surveyRow.id).catch(() => {});
              return { created: true, sent: res.ok };
            } catch (e) {
              console.error("[csat-dispatcher] tg send failed", e);
            }
          }
          return { created: true, sent: false };
        }),
      );

      for (const r of processResults) {
        if (r.created) created++;
        if (r.sent) sent++;
      }
    }

    return new Response(
      JSON.stringify({
        eligible: orders?.length ?? 0,
        surveys_created: created,
        telegram_sent: sent,
        skipped_existing: skipped,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("[csat-dispatcher] fatal", err);
    return new Response(JSON.stringify({ error: String((err as Error)?.message ?? err) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
