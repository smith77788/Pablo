import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
    const TELEGRAM_API_KEY_1 = Deno.env.get("TELEGRAM_API_KEY_1");
    if (!LOVABLE_API_KEY || !TELEGRAM_API_KEY_1) {
      return new Response(JSON.stringify({ error: "Missing API keys" }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    const body = await req.json();
    const { event_type, email, ip_address, user_agent, details } = body;

    // Get admin chat IDs
    const { data: roles } = await supabase
      .from("user_roles")
      .select("user_id")
      .in("role", ["admin"]);

    if (!roles || roles.length === 0) {
      return new Response(JSON.stringify({ ok: true, message: "No admins" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const { data: chats } = await supabase
      .from("telegram_chat_ids")
      .select("chat_id")
      .in("user_id", roles.map((r: any) => r.user_id));

    if (!chats || chats.length === 0) {
      return new Response(JSON.stringify({ ok: true, message: "No telegram chats" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const escapeHtml = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

    let text = `🚨 <b>СПОВІЩЕННЯ БЕЗПЕКИ</b>\n\n`;
    text += `⚠️ <b>Тип:</b> ${escapeHtml(event_type || "Unknown")}\n`;
    if (email) text += `📧 <b>Email:</b> ${escapeHtml(email)}\n`;
    if (ip_address) text += `🌐 <b>IP:</b> ${escapeHtml(ip_address)}\n`;
    if (user_agent) text += `🖥 <b>UA:</b> ${escapeHtml(user_agent.slice(0, 100))}\n`;
    if (details) text += `\n📝 <b>Деталі:</b> ${escapeHtml(String(details).slice(0, 500))}`;
    text += `\n\n⏰ ${new Date().toLocaleString("uk-UA", { timeZone: "Europe/Kyiv" })}`;

    await Promise.all(chats.map((chat) =>
      fetch(`${GATEWAY_URL}/sendMessage`, {
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
      }).catch(() => {})
    ));

    return new Response(JSON.stringify({ ok: true }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Security alert error:", error);
    return new Response(JSON.stringify({ error: String(error) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
