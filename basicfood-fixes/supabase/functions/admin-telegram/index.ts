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
        status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const authHeader = req.headers.get("Authorization");
    if (!authHeader) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseAnonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
    const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    const userClient = createClient(supabaseUrl, supabaseAnonKey, {
      global: { headers: { Authorization: authHeader } },
    });
    const { data: { user } } = await userClient.auth.getUser();
    if (!user) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const serviceClient = createClient(supabaseUrl, supabaseServiceKey);
    const { data: roles } = await serviceClient
      .from("user_roles")
      .select("role")
      .eq("user_id", user.id)
      .in("role", ["admin", "moderator"]);

    if (!roles || roles.length === 0) {
      return new Response(JSON.stringify({ error: "Forbidden" }), {
        status: 403, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const body = await req.json();
    const { action, chat_id, message, reply_markup, media_url, media_type, audience } = body;

    // ── Get audience counts (для UI попереднього перегляду) ──
    if (action === "audience_counts") {
      const { data: allChats } = await serviceClient
        .from("telegram_customer_chats")
        .select("chat_id")
        .eq("is_blocked", false);
      const allIds = (allChats || []).map((c: any) => Number(c.chat_id));

      // Wholesale: chats linked to a profile that is_wholesale=true
      const { data: wsProfiles } = await serviceClient
        .from("profiles")
        .select("user_id")
        .eq("is_wholesale", true);
      const wsUserIds = (wsProfiles || []).map((p: any) => p.user_id);
      let wsIds = new Set<number>();
      if (wsUserIds.length > 0) {
        const { data: wsLinks } = await serviceClient
          .from("telegram_chat_ids")
          .select("chat_id")
          .in("user_id", wsUserIds);
        wsIds = new Set((wsLinks || []).map((r: any) => Number(r.chat_id)));
      }

      const wholesale = allIds.filter((id) => wsIds.has(id)).length;
      return new Response(
        JSON.stringify({
          ok: true,
          all: allIds.length,
          wholesale,
          retail: allIds.length - wholesale,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // ── Send message to Telegram chat ──
    if (action === "send_telegram") {
      if (!chat_id || !message) {
        return new Response(JSON.stringify({ error: "chat_id and message required" }), {
          status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      const res = await fetch(`${GATEWAY_URL}/sendMessage`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${LOVABLE_API_KEY}`,
          "X-Connection-Api-Key": TELEGRAM_API_KEY_1,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          chat_id: Number(chat_id),
          text: `💬 <b>Відповідь від BASIC FOOD:</b>\n\n${message}`,
          parse_mode: "HTML",
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        return new Response(JSON.stringify({ error: "Telegram API error", details: data }), {
          status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      await serviceClient.from("telegram_messages").insert({
        chat_id: Number(chat_id),
        direction: "out",
        message_text: message,
        sender_name: "Менеджер",
      });

      return new Response(JSON.stringify({ ok: true }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // ── Broadcast to all Telegram customers ──
    if (action === "broadcast") {
      if (!message && !media_url) {
        return new Response(JSON.stringify({ error: "message or media required" }), {
          status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      const { data: customers } = await serviceClient
        .from("telegram_customer_chats")
        .select("chat_id, first_name")
        .eq("is_blocked", false);

      let recipients = customers || [];

      // Filter by audience: 'all' | 'wholesale' | 'retail'
      const aud = (audience || "all") as string;
      if (aud === "wholesale" || aud === "retail") {
        const { data: wsProfiles } = await serviceClient
          .from("profiles")
          .select("user_id")
          .eq("is_wholesale", true);
        const wsUserIds = (wsProfiles || []).map((p: any) => p.user_id);
        let wsChatIds = new Set<number>();
        if (wsUserIds.length > 0) {
          const { data: wsLinks } = await serviceClient
            .from("telegram_chat_ids")
            .select("chat_id")
            .in("user_id", wsUserIds);
          wsChatIds = new Set((wsLinks || []).map((r: any) => Number(r.chat_id)));
        }
        recipients = recipients.filter((c: any) =>
          aud === "wholesale" ? wsChatIds.has(Number(c.chat_id)) : !wsChatIds.has(Number(c.chat_id))
        );
      }

      if (recipients.length === 0) {
        return new Response(JSON.stringify({ ok: true, sent: 0, failed: 0, total: 0, audience: aud }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      let sent = 0;
      let failed = 0;
      const text = message ? `📢 <b>BASIC FOOD</b>\n\n${message}` : "";

      // Batched parallel broadcast — 25 per chunk, 1.1s between chunks.
      // Respects Telegram's 30 msg/s global rate limit and Supabase's 60s timeout.
      const BATCH_SIZE = 25;
      const BATCH_DELAY_MS = 1100;
      const tgHeaders = {
        Authorization: `Bearer ${LOVABLE_API_KEY}`,
        "X-Connection-Api-Key": TELEGRAM_API_KEY_1,
        "Content-Type": "application/json",
      };

      const sendOne = async (c: any): Promise<boolean> => {
        try {
          const chatId = Number(c.chat_id);
          let res: Response;
          if (media_url && media_type === "photo") {
            const msgBody: any = { chat_id: chatId, photo: media_url, parse_mode: "HTML" };
            if (text) msgBody.caption = text;
            if (reply_markup) msgBody.reply_markup = reply_markup;
            res = await fetch(`${GATEWAY_URL}/sendPhoto`, { method: "POST", headers: tgHeaders, body: JSON.stringify(msgBody) });
          } else if (media_url && media_type === "video") {
            const msgBody: any = { chat_id: chatId, video: media_url, parse_mode: "HTML" };
            if (text) msgBody.caption = text;
            if (reply_markup) msgBody.reply_markup = reply_markup;
            res = await fetch(`${GATEWAY_URL}/sendVideo`, { method: "POST", headers: tgHeaders, body: JSON.stringify(msgBody) });
          } else {
            const msgBody: any = { chat_id: chatId, text, parse_mode: "HTML" };
            if (reply_markup) msgBody.reply_markup = reply_markup;
            res = await fetch(`${GATEWAY_URL}/sendMessage`, { method: "POST", headers: tgHeaders, body: JSON.stringify(msgBody) });
          }
          return res.ok;
        } catch {
          return false;
        }
      };

      for (let i = 0; i < recipients.length; i += BATCH_SIZE) {
        const chunk = recipients.slice(i, i + BATCH_SIZE);
        const results = await Promise.all(chunk.map(sendOne));
        sent += results.filter(Boolean).length;
        failed += results.filter((r) => !r).length;
        if (i + BATCH_SIZE < recipients.length) {
          await new Promise((resolve) => setTimeout(resolve, BATCH_DELAY_MS));
        }
      }

      // Log broadcast for ROI attribution (consumed by acos-broadcast-roi)
      try {
        await serviceClient.from("events").insert({
          event_type: "broadcast_sent",
          source: "acos",
          metadata: {
            audience: aud,
            sent,
            failed,
            total: recipients.length,
            has_media: !!media_url,
            media_type: media_type ?? null,
            message_preview: (message ?? "").slice(0, 200),
            chat_ids: recipients.map((c: any) => Number(c.chat_id)),
          },
        });
      } catch (_logErr) {
        // analytics insert must not break broadcast
      }

      return new Response(JSON.stringify({ ok: true, sent, failed, total: recipients.length, audience: aud }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "Unknown action" }), {
      status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: String(err) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
