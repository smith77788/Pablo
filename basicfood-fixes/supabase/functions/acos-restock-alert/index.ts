import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

async function sendTelegram(chatId: number, text: string) {
  const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
  const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY");
  if (!LOVABLE_API_KEY || !TELEGRAM_API_KEY) return;
  await fetch(`${GATEWAY_URL}/sendMessage`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${LOVABLE_API_KEY}`,
      "X-Connection-Api-Key": TELEGRAM_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" }),
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // Cooldown: don't repeat alerts within 24h
    const since24 = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const { data: recent } = await supabase
      .from("ai_insights")
      .select("id")
      .eq("insight_type", "restock_alert_sent")
      .gte("created_at", since24);
    if ((recent?.length ?? 0) > 0) {
      return new Response(JSON.stringify({ skipped: "cooldown" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Get latest inventory_forecast insight
    const { data: forecast } = await supabase
      .from("ai_insights")
      .select("metrics, created_at")
      .eq("insight_type", "inventory_forecast")
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    const alerts = (forecast?.metrics as any)?.alerts as Array<{ name: string; stock: number; days_left: number; daily_velocity: number; level: string }> | undefined;
    const critical = (alerts ?? []).filter((a) => a.level === "critical");

    if (critical.length === 0) {
      return new Response(JSON.stringify({ sent: 0, reason: "no_critical" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Get admin chat IDs
    const { data: admins } = await supabase
      .from("user_roles")
      .select("user_id")
      .eq("role", "admin");
    const adminIds = (admins ?? []).map((a) => a.user_id);
    if (adminIds.length === 0) {
      return new Response(JSON.stringify({ sent: 0, reason: "no_admins" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    const { data: chats } = await supabase
      .from("telegram_chat_ids")
      .select("chat_id")
      .in("user_id", adminIds);

    const lines = critical
      .map((a) => `• <b>${a.name}</b> — ${a.days_left}д (stock: ${a.stock}, ~${Math.ceil(a.daily_velocity * 14)} шт на 2 тижні)`)
      .join("\n");
    const text = `⚠️ <b>RESTOCK ALERT</b>\n\nКритичний рівень запасів (≤3 днів):\n\n${lines}\n\nРекомендую замовити постачання найближчим часом.`;

    const results = await Promise.all(
      (chats ?? []).map(async (c) => {
        try { await sendTelegram(Number(c.chat_id), text); return true; } catch { return false; }
      })
    );
    const sent = results.filter(Boolean).length;

    await supabase.from("ai_insights").insert({
      insight_type: "restock_alert_sent",
      title: `📨 Restock alert надіслано (${critical.length} товарів)`,
      description: `Критичні товари: ${critical.map((c) => c.name).join(", ")}`,
      confidence: 1,
      affected_layer: "inventory",
      risk_level: "high",
      status: "new",
      metrics: { sent_to_chats: sent, critical_count: critical.length },
    });

    return new Response(JSON.stringify({ sent, critical: critical.length }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
