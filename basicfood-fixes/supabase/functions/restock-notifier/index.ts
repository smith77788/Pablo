// Restock Notifier — fires when products come back in stock.
// Triggered by trg_queue_restock_alerts (DB trigger) which inserts pending
// rows into restock_alerts. This function picks up unsent alerts and pushes
// notifications via in-app bell + Telegram (if user is linked).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

const TG_BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "";
const SITE_URL = "https://basic-food.shop";

async function sendTelegram(chatId: number, text: string, productId: string): Promise<boolean> {
  if (!TG_BOT_TOKEN) return false;
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: false,
        reply_markup: {
          inline_keyboard: [[
            { text: "🛒 Купити зараз", url: `${SITE_URL}/product/${productId}` },
          ]],
        },
      }),
    });
    return res.ok;
  } catch (e) {
    console.error("Telegram send error:", e);
    return false;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    // Pending alerts (created in last 1 hour, not notified yet)
    const { data: pending, error } = await supabase
      .from("restock_alerts")
      .select("id, user_id, product_id, product_name")
      .is("notified_at", null)
      .gte("created_at", new Date(Date.now() - 3600_000).toISOString())
      .limit(500);
    if (error) throw error;

    if (!pending || pending.length === 0) {
      return new Response(JSON.stringify({ ok: true, processed: 0 }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } });
    }

    // Fetch telegram chat ids for these users in bulk
    const userIds = [...new Set(pending.map(p => p.user_id))];
    const { data: tgChats } = await supabase
      .from("telegram_chat_ids")
      .select("user_id, chat_id")
      .in("user_id", userIds);
    const chatByUser = new Map<string, number>();
    for (const t of tgChats ?? []) chatByUser.set(t.user_id, Number(t.chat_id));

    // 1) Batch insert all in-app notifications.
    const { error: batchNotifErr } = await supabase.from("notifications").insert(
      pending.map((alert: any) => ({
        user_id: alert.user_id,
        type: "restock",
        title: "🎉 Товар знову в наявності!",
        message: `"${alert.product_name}" з твого вішлиста знову можна замовити.`,
        reference_id: alert.product_id,
      })),
    );
    let inApp = batchNotifErr ? 0 : pending.length;

    // 2) Parallel Telegram sends + collect per-alert channel results.
    const notifiedAt = new Date().toISOString();
    type AlertResult = { alert: any; channels: string[] };
    const alertResults: AlertResult[] = await Promise.all(
      pending.map(async (alert: any) => {
        const channels: string[] = inApp > 0 ? ["in_app"] : [];
        const chatId = chatByUser.get(alert.user_id);
        if (chatId) {
          const text =
            `🎉 <b>Знову в наявності!</b>\n\n` +
            `Товар <b>${alert.product_name}</b> з твого вішлиста повернувся.\n` +
            `Замовляй, поки є — рекомендуємо не зволікати 🐶`;
          const ok = await sendTelegram(chatId, text, alert.product_id);
          if (ok) channels.push("telegram");
        }
        return { alert, channels };
      }),
    );

    let tg = alertResults.filter((r) => r.channels.includes("telegram")).length;
    let failed = 0;

    // 3) Parallel restock_alerts status updates.
    const updateResults = await Promise.all(
      alertResults.map(({ alert, channels }) =>
        supabase.from("restock_alerts").update({
          notified_at: notifiedAt,
          notification_channel: channels.join(",") || "none",
        }).eq("id", alert.id),
      ),
    );
    failed = updateResults.filter((r) => r.error).length;

    // Drop a low-priority insight (info-level, ignored status)
    await supabase.from("ai_insights").insert({
      insight_type: "restock_notification_batch",
      affected_layer: "marketing",
      risk_level: "low",
      title: `Restock: сповіщено ${pending.length} клієнтів`,
      description: `Товари повернулись у наявність. In-app: ${inApp}, Telegram: ${tg}, Failed: ${failed}`,
      expected_impact: `Очікувана конверсія 25-40% з ${pending.length} = ${Math.round(pending.length * 0.3)} замовлень`,
      confidence: 0.75,
      status: "ignored",
      metrics: {
        fingerprint: `restock_${new Date().toISOString().slice(0,16)}`,
        total: pending.length, in_app: inApp, telegram: tg, failed,
        generated_by: "restock-notifier",
        generated_at: new Date().toISOString(),
      },
    });

    return new Response(
      JSON.stringify({ ok: true, processed: pending.length, in_app: inApp, telegram: tg, failed }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    console.error("restock-notifier error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
