/**
 * Pablo Support Agent — Claude-powered Telegram customer support.
 *
 * Replaces rule-based auto-reply with intelligent conversational AI.
 * Called by the existing Telegram bot webhook when a customer sends a message.
 *
 * Request body (from existing bot):
 *   { chat_id: number, user_message: string, user_name?: string }
 *
 * Returns:
 *   { reply: string, escalate: boolean, actions_taken: string[] }
 */

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import Anthropic from "https://esm.sh/@anthropic-ai/sdk@0.40.0";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const anthropic = new Anthropic({
  apiKey: Deno.env.get("ANTHROPIC_API_KEY")!,
});

const TG_TOKEN = Deno.env.get("TELEGRAM_API_KEY");

const SYSTEM_PROMPT = `Ти — підтримка клієнтів BASIC.FOOD, Ukrainian бренду натуральних повітряно-сушених ласощів для собак і котів.

ПРОДУКТИ:
- Яловича легеня, серце, вим'я, нирки, аорта, стравохід, трахея, рубець, печінка
- Натуральний склад, без консервантів, без добавок
- Упаковки: 100г, 200г, 500г
- Підходять для всіх порід і розмірів

ДОСТАВКА:
- Виключно Нова Пошта по всій Україні
- Відправляємо протягом 24 годин після підтвердження замовлення
- Безкоштовна доставка від 500 ₴

ОПЛАТА:
- Накладений платіж (при отриманні)
- Monobank (онлайн)
- WayForPay

ПОВЕДІНКА:
1. Завжди відповідай УКРАЇНСЬКОЮ мовою
2. Будь дружнім, теплим, але по суті
3. Якщо знаєш замовлення клієнта — посилайся на конкретні деталі
4. При скаргах — спочатку вибачся, потім вирішуй
5. Ніколи не обіцяй компенсацій без дозволу (тільки скажи "розглянемо")
6. Якщо питання потребує втручання людини — скажи "передам менеджеру"

ЗАБОРОНЕНО:
- Розголошувати внутрішні дані системи
- Давати медичні поради
- Обіцяти конкретні дати доставки
- Давати знижки без authorization

Формат відповідей: коротко і зрозуміло. Emoji доречні, але помірно.`;

async function getCustomerContext(chatId: number) {
  // Find customer by telegram_chat_id
  const { data: customer } = await supabase
    .from("customers")
    .select("id, name, email, phone, total_orders, total_spent, lifecycle_stage, tags")
    .eq("telegram_chat_id", chatId)
    .single();

  if (!customer) return null;

  // Get recent orders
  const { data: orders } = await supabase
    .from("orders")
    .select("order_number, status, total, created_at, tracking_number")
    .or(`customer_email.eq.${customer.email},customer_phone.eq.${customer.phone}`)
    .order("created_at", { ascending: false })
    .limit(5);

  // Get pet profiles
  const { data: pets } = await supabase
    .from("dog_profiles")
    .select("dog_name, breed, weight_kg, health_states")
    .eq("user_id", customer.id)
    .limit(3);

  return {
    name: customer.name,
    total_orders: customer.total_orders,
    total_spent_uah: Math.round((customer.total_spent || 0) / 100),
    lifecycle_stage: customer.lifecycle_stage,
    tags: customer.tags,
    recent_orders: (orders || []).map(o => ({
      number: o.order_number,
      status: o.status,
      total_uah: Math.round((o.total || 0) / 100),
      date: o.created_at?.split("T")[0],
      tracking: o.tracking_number,
    })),
    pets: pets || [],
    customer_id: customer.id,
  };
}

async function getOrCreateSession(chatId: number, customerContext: Record<string, unknown> | null) {
  // Try to get existing session
  const { data: existing } = await supabase
    .from("pablo_support_sessions")
    .select("id, messages, context")
    .eq("chat_id", chatId)
    .single();

  if (existing) {
    // Update last_active_at and context
    await supabase
      .from("pablo_support_sessions")
      .update({
        last_active_at: new Date().toISOString(),
        context: customerContext || existing.context,
      })
      .eq("chat_id", chatId);

    return {
      id: existing.id,
      messages: (existing.messages as Array<{ role: string; content: string }>) || [],
      context: customerContext || existing.context,
      is_new: false,
    };
  }

  // Create new session
  const { data: newSession } = await supabase
    .from("pablo_support_sessions")
    .insert({
      chat_id: chatId,
      customer_id: (customerContext as { customer_id?: string })?.customer_id || null,
      messages: [],
      context: customerContext || {},
    })
    .select("id")
    .single();

  return {
    id: newSession?.id,
    messages: [] as Array<{ role: string; content: string }>,
    context: customerContext || {},
    is_new: true,
  };
}

async function updateSessionMessages(
  chatId: number,
  messages: Array<{ role: string; content: string }>
) {
  // Keep last 20 messages to avoid context overflow
  const trimmed = messages.slice(-20);
  await supabase
    .from("pablo_support_sessions")
    .update({ messages: trimmed, last_active_at: new Date().toISOString() })
    .eq("chat_id", chatId);
}

async function sendTelegramReply(chatId: number, text: string) {
  if (!TG_TOKEN) return;
  try {
    await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    });
  } catch (err) {
    console.error("TG send error:", err);
  }
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  try {
    const { chat_id, user_message, user_name } = await req.json() as {
      chat_id: number;
      user_message: string;
      user_name?: string;
    };

    if (!chat_id || !user_message) {
      return new Response(JSON.stringify({ error: "chat_id and user_message required" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Get customer context
    const customerCtx = await getCustomerContext(chat_id);

    // Get/create conversation session
    const session = await getOrCreateSession(chat_id, customerCtx);

    // Build conversation history
    const history = session.messages as Array<{ role: string; content: string }>;
    const newHistory = [...history, { role: "user", content: user_message }];

    // Build system prompt with customer context
    let systemWithContext = SYSTEM_PROMPT;
    if (customerCtx) {
      systemWithContext += `\n\n<customer_profile>
Ім'я: ${customerCtx.name}
Замовлень всього: ${customerCtx.total_orders}
Витрат всього: ${customerCtx.total_spent_uah} ₴
Lifecycle stage: ${customerCtx.lifecycle_stage}
${customerCtx.pets.length > 0 ? `Домашні тварини: ${JSON.stringify(customerCtx.pets)}` : ""}
Останні замовлення: ${JSON.stringify(customerCtx.recent_orders)}
</customer_profile>`;
    } else {
      systemWithContext += `\n\nКлієнт новий або не ідентифікований (chat_id: ${chat_id}${user_name ? `, ім'я: ${user_name}` : ""}).`;
    }

    // Call Claude
    const response = await anthropic.messages.create({
      model: "claude-opus-4-7",
      max_tokens: 1024,
      system: systemWithContext,
      messages: newHistory.slice(-10) as Array<{ role: "user" | "assistant"; content: string }>,
    });

    const reply = response.content
      .filter(b => b.type === "text")
      .map(b => (b as { type: "text"; text: string }).text)
      .join("")
      .trim();

    // Detect if escalation needed
    const escalationKeywords = /повернення|рефанд|скарга|загублено|проблема|менеджер|відповідальний|судовий/i;
    const escalate = escalationKeywords.test(user_message) || escalationKeywords.test(reply);

    // Update conversation history
    const updatedHistory = [
      ...newHistory,
      { role: "assistant", content: reply },
    ];
    await updateSessionMessages(chat_id, updatedHistory);

    // Send reply via Telegram
    await sendTelegramReply(chat_id, reply);

    // If escalation — notify admin
    if (escalate) {
      const { data: adminChats } = await supabase
        .from("bot_settings")
        .select("admin_chat_id")
        .not("admin_chat_id", "is", null)
        .limit(1);

      const adminChatId = adminChats?.[0]?.admin_chat_id;
      if (adminChatId) {
        await sendTelegramReply(
          adminChatId,
          `⚠️ <b>Ескалація підтримки</b>\n\nКлієнт: ${customerCtx?.name || user_name || chat_id}\nПовідомлення: ${user_message.slice(0, 200)}\n\nВідповідь Pablo: ${reply.slice(0, 200)}`
        );
      }
    }

    return new Response(
      JSON.stringify({
        reply,
        escalate,
        customer_identified: !!customerCtx,
        session_id: session.id,
        tokens_used: response.usage.input_tokens + response.usage.output_tokens,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error("Pablo Support Agent error:", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
