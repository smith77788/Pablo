// Two-stage post-checkout flow for unpaid Monobank orders.
//
// Runs every 5 min via pg_cron.
//
// Stage 1 (15 min after creation, status='new', payment_method='card_online'):
//   Send a soft Telegram reminder to the customer with a "Pay now" inline button
//   pointing to the saved Monobank pageUrl. Marked in admin_notes as `reminder_sent`.
//
// Stage 2 (30 min after creation, still unpaid):
//   Verify status via Monobank /invoice/status. If actually paid/processing — skip.
//   Otherwise: cancel the invoice in Monobank and mark the order as `cancelled`.
//   Notify the customer (Telegram, if linked) and admins/managers.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MONO_STATUS_URL = "https://api.monobank.ua/api/merchant/invoice/status";
const MONO_CANCEL_URL = "https://api.monobank.ua/api/merchant/invoice/cancel";
const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";
const REMINDER_MINUTES = 15;
const CANCEL_MINUTES = 30;

function escapeHtml(str: string): string {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function extractInvoiceId(notes: string | null): string | null {
  if (!notes) return null;
  const m = notes.match(/mono_invoice:([A-Za-z0-9_-]+)/);
  return m ? m[1] : null;
}

function extractPageUrl(notes: string | null): string | null {
  if (!notes) return null;
  const m = notes.match(/mono_page:(\S+)/);
  return m ? m[1] : null;
}

async function tgSend(
  chatId: number | string,
  text: string,
  lovableKey: string,
  tgKey: string,
  replyMarkup?: any,
) {
  try {
    await fetch(`${GATEWAY_URL}/sendMessage`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${lovableKey}`,
        "X-Connection-Api-Key": tgKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
        ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
      }),
    });
  } catch (e) {
    console.error("TG send error:", e);
  }
}

async function findCustomerChatId(
  supabase: any,
  phone: string | null,
  email: string | null,
): Promise<number | null> {
  const filters = [
    phone ? `phone.eq.${phone}` : null,
    email ? `email.eq.${email}` : null,
  ].filter(Boolean);
  if (filters.length === 0) return null;
  const { data: customer } = await supabase
    .from("customers")
    .select("telegram_chat_id")
    .or(filters.join(","))
    .maybeSingle();
  return customer?.telegram_chat_id ?? null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const monoToken = Deno.env.get("MONOBANK_ACQUIRING_TOKEN");
    const lovableKey = Deno.env.get("LOVABLE_API_KEY");
    const tgKey = Deno.env.get("TELEGRAM_API_KEY_1");
    if (!monoToken) throw new Error("MONOBANK_ACQUIRING_TOKEN is not configured");

    const supabase = createClient(supabaseUrl, serviceKey);
    const tgConfigured = Boolean(lovableKey && tgKey);

    const now = Date.now();
    const reminderCutoff = new Date(now - REMINDER_MINUTES * 60_000).toISOString();
    const cancelCutoff = new Date(now - CANCEL_MINUTES * 60_000).toISOString();

    // Pull all unpaid card_online orders older than 15 min — covers both stages
    const { data: orders, error } = await supabase
      .from("orders")
      .select("id, customer_name, customer_phone, customer_email, total, admin_notes, created_at")
      .eq("status", "new")
      .eq("payment_method", "card_online")
      .lt("created_at", reminderCutoff);
    if (error) throw error;

    if (!orders || orders.length === 0) {
      return new Response(JSON.stringify({ ok: true, checked: 0, reminded: 0, cancelled: 0 }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Pre-load admin chat ids once
    let adminChats: { chat_id: number }[] = [];
    if (tgConfigured) {
      const { data: chatRecords } = await supabase.from("telegram_chat_ids").select("chat_id, user_id");
      const { data: roles } = await supabase
        .from("user_roles")
        .select("user_id")
        .in("role", ["admin", "moderator"]);
      const adminIds = new Set((roles || []).map((r: any) => r.user_id));
      adminChats = (chatRecords || []).filter((c: any) => adminIds.has(c.user_id));
    }

    let reminded = 0;
    let cancelled = 0;
    const results: any[] = [];

    for (const order of orders) {
      const isStaleEnoughToCancel = new Date(order.created_at).getTime() <= new Date(cancelCutoff).getTime();
      const reminderAlreadySent = order.admin_notes?.includes("reminder_sent");

      // ── Stage 2: cancel orders > 30 min ──
      if (isStaleEnoughToCancel) {
        const invoiceId = extractInvoiceId(order.admin_notes);
        if (!invoiceId) {
          results.push({ id: order.id, skipped: "no_invoice_id" });
          continue;
        }

        let monoStatus: string | null = null;
        try {
          const r = await fetch(`${MONO_STATUS_URL}?invoiceId=${encodeURIComponent(invoiceId)}`, {
            headers: { "X-Token": monoToken },
          });
          const d = await r.json();
          monoStatus = d.status ?? null;
          console.log(`Cancel-check ${order.id} invoice ${invoiceId}: ${monoStatus}`);
        } catch (e) {
          console.error("checkInvoice error:", e);
          results.push({ id: order.id, error: "status_check_failed" });
          continue;
        }

        if (monoStatus === "success" || monoStatus === "hold" || monoStatus === "processing") {
          results.push({ id: order.id, skipped: `mono_status:${monoStatus}` });
          continue;
        }

        try {
          await fetch(MONO_CANCEL_URL, {
            method: "POST",
            headers: { "X-Token": monoToken, "Content-Type": "application/json" },
            body: JSON.stringify({ invoiceId }),
          });
        } catch (e) {
          console.warn("cancelInvoice warn:", e);
        }

        await supabase
          .from("orders")
          .update({
            status: "cancelled",
            admin_notes: `${order.admin_notes ?? ""} auto_cancelled:stale_30m mono_status:${monoStatus ?? "unknown"}`.trim(),
          })
          .eq("id", order.id);
        cancelled++;
        results.push({ id: order.id, cancelled: true, mono_status: monoStatus });

        if (tgConfigured) {
          const customerChatId = await findCustomerChatId(supabase, order.customer_phone, order.customer_email);
          if (customerChatId) {
            await tgSend(
              customerChatId,
              `⚠️ <b>Замовлення скасовано через неоплату</b>\n\n` +
                `🆔 #${escapeHtml(order.id.slice(0, 8))}\n` +
                `💰 Сума: ${order.total} ₴\n\n` +
                `Ми не отримали оплату протягом 30 хвилин, тож замовлення автоматично скасовано.\n\n` +
                `Якщо ви все ще хочете замовити — просто оформіть замовлення повторно 🙌\n` +
                `<a href="https://basic-food.shop">basic-food.shop</a>`,
              lovableKey!,
              tgKey!,
            );
          }

          const adminText =
            `🚫 <b>Авто-скасування неоплаченого замовлення</b>\n\n` +
            `🆔 #${escapeHtml(order.id.slice(0, 8))}\n` +
            `👤 ${escapeHtml(String(order.customer_name))}\n` +
            (order.customer_phone ? `📱 ${escapeHtml(String(order.customer_phone))}\n` : "") +
            `💰 ${order.total} ₴\n` +
            `📊 Статус Monobank: ${monoStatus ?? "невідомо"}\n` +
            (customerChatId ? `\n✉️ Клієнту надіслано сповіщення в Telegram` : `\n⚠️ У клієнта немає привʼязаного Telegram — варто передзвонити`);
          for (const chat of adminChats) {
            await tgSend(chat.chat_id, adminText, lovableKey!, tgKey!);
          }
        }
        continue;
      }

      // ── Stage 1: soft reminder at 15 min (only once) ──
      if (reminderAlreadySent) {
        results.push({ id: order.id, skipped: "reminder_already_sent" });
        continue;
      }

      if (!tgConfigured) {
        results.push({ id: order.id, skipped: "telegram_not_configured" });
        continue;
      }

      const customerChatId = await findCustomerChatId(supabase, order.customer_phone, order.customer_email);
      const pageUrl = extractPageUrl(order.admin_notes);

      if (customerChatId && pageUrl) {
        await tgSend(
          customerChatId,
          `🔔 <b>Нагадуємо про оплату</b>\n\n` +
            `Здається, оплата за замовлення #${escapeHtml(order.id.slice(0, 8))} ще не пройшла.\n\n` +
            `💰 Сума: <b>${order.total} ₴</b>\n\n` +
            `Натисніть кнопку нижче, щоб завершити оплату — посилання активне ще ~15 хв 🙏`,
          lovableKey!,
          tgKey!,
          {
            inline_keyboard: [[{ text: "💳 Сплатити зараз", url: pageUrl }]],
          },
        );
        reminded++;
        results.push({ id: order.id, reminded: true });
      } else {
        results.push({
          id: order.id,
          skipped: !customerChatId ? "no_customer_telegram" : "no_page_url",
        });
      }

      // Mark reminder as sent so we don't spam, even if customer had no Telegram
      await supabase
        .from("orders")
        .update({ admin_notes: `${order.admin_notes ?? ""} reminder_sent:${customerChatId ? "tg" : "none"}`.trim() })
        .eq("id", order.id);
    }

    return new Response(
      JSON.stringify({ ok: true, checked: orders.length, reminded, cancelled, results }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 200 },
    );
  } catch (e: any) {
    console.error("cancel-stale-orders error:", e);
    return new Response(JSON.stringify({ error: e.message }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
      status: 500,
    });
  }
});
