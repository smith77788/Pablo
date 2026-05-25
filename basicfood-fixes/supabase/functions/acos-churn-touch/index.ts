// Sends an immediate, personalized churn-recovery message to a single
// VIP. Used by AcosChurnRiskPanel when the manager wants to override
// the standard winback cadence.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const TG_BOT_TOKEN = Deno.env.get("TELEGRAM_API_KEY") ?? Deno.env.get("TELEGRAM_API_KEY_1");
const PROMO_DISCOUNT_PCT = 15;
const PROMO_MIN_ORDER = 300;
const PROMO_TTL_HOURS = 48;

const generateCode = () =>
  "VIP" + Math.random().toString(36).slice(2, 8).toUpperCase();

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
  const __agent = beginQuickAgentRun("acos-churn-touch", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));
    const customerId = String(body.customer_id ?? "");
    if (!customerId) {
      return new Response(
        JSON.stringify({ error: "customer_id required" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const { data: customer, error: cErr } = await supabase
      .from("customers")
      .select("id, name, telegram_chat_id, total_orders, tags")
      .eq("id", customerId)
      .single();

    if (cErr || !customer) {
      return new Response(
        JSON.stringify({ error: "customer not found" }),
        { status: 404, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (!customer.telegram_chat_id) {
      return new Response(
        JSON.stringify({ error: "customer has no telegram_chat_id" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 1. Mint promo code.
    const code = generateCode();
    const expiresAt = new Date(Date.now() + PROMO_TTL_HOURS * 60 * 60 * 1000).toISOString();
    const { data: promo, error: pErr } = await supabase
      .from("promo_codes")
      .insert({
        code,
        discount_type: "percentage",
        discount_value: PROMO_DISCOUNT_PCT,
        max_uses: 1,
        min_order_amount: PROMO_MIN_ORDER,
        starts_at: new Date().toISOString(),
        ends_at: expiresAt,
        is_active: true,
      })
      .select("id, code")
      .single();

    if (pErr || !promo) {
      return new Response(
        JSON.stringify({ error: "promo create failed", detail: pErr?.message }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Compose & send Telegram message.
    const firstName = (customer.name ?? "друже").split(" ")[0];
    const msg =
      `💎 <b>${firstName}, тільки для вас як VIP клієнта</b>\n\n` +
      `Ви з нами вже ${customer.total_orders} замовлень — і ми хочемо подякувати персональною пропозицією 🎁\n\n` +
      `🎫 <b>Промокод:</b>\n<code>${code}</code>\n\n` +
      `💰 Знижка <b>−${PROMO_DISCOUNT_PCT}%</b> на наступне замовлення\n` +
      `🛒 Мінімум: ${PROMO_MIN_ORDER} ₴\n` +
      `⏰ Дійсний <b>${PROMO_TTL_HOURS} години</b> (одноразовий)\n\n` +
      `🔗 <a href="https://basic-food.shop/catalog">Перейти до каталогу</a>`;

    const ok = await sendTelegram(Number(customer.telegram_chat_id), msg);
    if (!ok) {
      // Deactivate orphaned promo — nobody received the code
      await supabase.from("promo_codes").update({ is_active: false }).eq("id", promo.id).catch(() => {});
      return new Response(
        JSON.stringify({ error: "telegram send failed" }),
        { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 3. Log event for cooldown tracking + clear churn_risk tag (touched).
    await supabase.from("events").insert({
      event_type: "winback_sent",
      source: "acos_manual_churn_touch",
      metadata: {
        customer_id: customer.id,
        chat_id: Number(customer.telegram_chat_id),
        promo_code: promo.code,
        promo_id: promo.id,
        manual: true,
      },
    });

    const nextTags = (customer.tags ?? []).filter(
      (t: string) => !t.startsWith("churn_risk:"),
    );
    await supabase.from("customers").update({ tags: nextTags }).eq("id", customer.id);

    __agent.success();
    return new Response(
      JSON.stringify({ sent: true, code: promo.code, expires_at: expiresAt }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    __agent.error(err);
        return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
