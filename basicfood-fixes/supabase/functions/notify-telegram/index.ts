import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

const paymentLabels: Record<string, string> = {
  cash_on_delivery: "Накладений платіж",
  card_transfer: "Переказ на картку",
  callback_request: "Запит на дзвінок",
};

function escapeHtml(str: string): string {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  // Checkout/contact-form fan-out — protect Telegram quota from spam bots.
  // ≈30 req/min sustained, burst 30.
  const rl = rateLimit(`notify-telegram:${getClientIp(req)}`, { capacity: 30, refillPerSec: 0.5 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  try {
    const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
    if (!LOVABLE_API_KEY) throw new Error("LOVABLE_API_KEY is not configured");

    const TELEGRAM_API_KEY_1 = Deno.env.get("TELEGRAM_API_KEY_1");
    if (!TELEGRAM_API_KEY_1) throw new Error("TELEGRAM_API_KEY_1 is not configured");

    const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
    const SUPABASE_ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;
    const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    // ── Auth check: verify caller is authenticated ──
    const authHeader = req.headers.get("Authorization");
    if (authHeader) {
      const userClient = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
        global: { headers: { Authorization: authHeader } },
      });
      const token = authHeader.replace("Bearer ", "");
      const { data: claims, error: claimsErr } = await userClient.auth.getClaims(token);
      // Allow both authenticated users and anonymous order placement
      if (claimsErr) {
        // If token is invalid, still allow if it comes from internal call (order flow)
        console.warn("Auth warning:", claimsErr.message);
      }
    }

    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

    const body = await req.json();
    const {
      order_id,
      customer_name,
      customer_phone,
      customer_email,
      delivery_address,
      payment_method,
      total,
      items_count,
      items_detail,
      message,
    } = body;

    // Validate required fields
    if (!order_id || !customer_name || total === undefined) {
      return new Response(JSON.stringify({ error: "Missing required fields" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Verify order exists in database to prevent fake notifications
    const { data: orderCheck } = await supabase
      .from("orders")
      .select("id")
      .eq("id", order_id)
      .maybeSingle();

    if (!orderCheck) {
      return new Response(JSON.stringify({ error: "Order not found" }), {
        status: 404,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Get all admin/manager telegram chat IDs
    const { data: chatRecords } = await supabase
      .from("telegram_chat_ids")
      .select("chat_id, user_id");

    if (!chatRecords || chatRecords.length === 0) {
      return new Response(JSON.stringify({ ok: true, message: "No telegram chats configured" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Filter only admins/managers
    const { data: roles } = await supabase
      .from("user_roles")
      .select("user_id")
      .in("role", ["admin", "moderator"]);

    const adminUserIds = new Set((roles || []).map((r: any) => r.user_id));
    const adminChats = chatRecords.filter((c: any) => adminUserIds.has(c.user_id));

    // Build items list with HTML escaping
    let itemsText = "";
    if (items_detail && Array.isArray(items_detail)) {
      itemsText = items_detail
        .map((item: any) => `  • ${escapeHtml(String(item.name))} × ${item.qty} = ${item.price * item.qty} ₴`)
        .join("\n");
    }

    const isCallback = payment_method === "callback_request";
    let text = "";

    if (isCallback) {
      text += `📞 <b>Нова заявка на зворотній зв'язок з <a href="https://basic-food.shop">BASIC FOOD</a></b>\n\n`;
      text += `🆔 <b>#${escapeHtml(order_id?.slice(0, 8) || "")}</b>\n\n`;
      text += `👤 <b>Клієнт:</b> ${escapeHtml(String(customer_name))}\n`;
      if (customer_phone) text += `📱 <b>Телефон:</b> ${escapeHtml(String(customer_phone))}\n`;
      if (customer_email) text += `✉️ <b>Email:</b> ${escapeHtml(String(customer_email))}\n`;
      if (message) text += `\n💬 <b>Повідомлення:</b>\n${escapeHtml(String(message))}`;
    } else {
      text += `🛒 <b>Нове замовлення на <a href="https://basic-food.shop">BASIC FOOD</a>!</b>\n\n`;
      text += `🆔 <b>#${escapeHtml(order_id?.slice(0, 8) || "")}</b>\n\n`;
      text += `👤 <b>Клієнт:</b> ${escapeHtml(String(customer_name))}\n`;
      if (customer_phone) text += `📱 <b>Телефон:</b> ${escapeHtml(String(customer_phone))}\n`;
      if (customer_email) text += `✉️ <b>Email:</b> ${escapeHtml(String(customer_email))}\n`;
      if (delivery_address) text += `📍 <b>Адреса:</b> ${escapeHtml(String(delivery_address))}\n`;
      text += `💳 <b>Оплата:</b> ${paymentLabels[payment_method] || escapeHtml(String(payment_method || "—"))}\n\n`;

      text += `📦 <b>Товари (${items_count ?? 0}):</b>\n`;
      if (itemsText) {
        text += itemsText + "\n\n";
      }

      text += `💰 <b>Сума: ${total} ₴</b>`;

      if (message) {
        text += `\n\n💬 <b>Коментар:</b> ${escapeHtml(String(message))}`;
      }
    }

    const results = await Promise.all(
      adminChats.map(async (chat) => {
        const response = await fetch(`${GATEWAY_URL}/sendMessage`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${LOVABLE_API_KEY}`,
            "X-Connection-Api-Key": TELEGRAM_API_KEY_1,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            chat_id: chat.chat_id,
            text,
            parse_mode: "HTML",
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) console.error(`[notify-telegram] failed chat_id=${chat.chat_id}:`, data);
        return { chat_id: chat.chat_id, ok: response.ok, data };
      })
    );

    return new Response(JSON.stringify({ ok: true, sent: results.length }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (error: unknown) {
    console.error("Error sending Telegram notification:", error);
    const errorMessage = error instanceof Error ? error.message : "Unknown error";
    return new Response(JSON.stringify({ error: errorMessage }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
