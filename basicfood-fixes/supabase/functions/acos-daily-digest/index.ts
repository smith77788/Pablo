// ACOS Daily Digest — sends top insights + KPI delta to admin Telegram chats.
// Triggered by pg_cron at 09:00 Kyiv (06:00 UTC summer / 07:00 UTC winter — we use 06:00 UTC).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const TG_TOKEN = Deno.env.get("TELEGRAM_API_KEY");

type FunnelCounts = { product_viewed: number; add_to_cart: number; begin_checkout: number; purchase_completed: number };

async function countEvents(eventType: string, fromIso: string, toIso: string): Promise<number> {
  const { count } = await supabase
    .from("events")
    .select("*", { count: "exact", head: true })
    .eq("event_type", eventType)
    .gte("created_at", fromIso)
    .lt("created_at", toIso);
  return count ?? 0;
}

async function getFunnel(fromIso: string, toIso: string): Promise<FunnelCounts> {
  const [pv, atc, bc, pc] = await Promise.all([
    countEvents("product_viewed", fromIso, toIso),
    countEvents("add_to_cart", fromIso, toIso),
    countEvents("begin_checkout", fromIso, toIso),
    countEvents("purchase_completed", fromIso, toIso),
  ]);
  return { product_viewed: pv, add_to_cart: atc, begin_checkout: bc, purchase_completed: pc };
}

async function getRevenue(fromIso: string, toIso: string): Promise<{ revenue: number; orders: number }> {
  const { data } = await supabase
    .from("orders")
    .select("total")
    .gte("created_at", fromIso)
    .lt("created_at", toIso)
    .neq("status", "cancelled");
  const revenue = (data ?? []).reduce((sum, o) => sum + (o.total ?? 0), 0);
  return { revenue, orders: data?.length ?? 0 };
}

function deltaPct(today: number, yesterday: number): string {
  if (yesterday === 0) return today > 0 ? "🆕" : "—";
  const pct = Math.round(((today - yesterday) / yesterday) * 100);
  if (pct === 0) return "→ 0%";
  return pct > 0 ? `📈 +${pct}%` : `📉 ${pct}%`;
}

function fmtNum(n: number): string {
  return n.toLocaleString("uk-UA");
}

async function sendTelegram(chatId: number, text: string): Promise<boolean> {
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
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
  } catch (err) {
    console.error("sendTelegram failed", err);
    return false;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  if (!TG_TOKEN) {
    return new Response(JSON.stringify({ error: "TELEGRAM_API_KEY missing" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  try {
    return await withAgentRun("acos-daily-digest", detectTrigger(req, null), async () => {
    const now = new Date();
    const startToday = new Date(now);
    startToday.setUTCHours(0, 0, 0, 0);
    const startYesterday = new Date(startToday);
    startYesterday.setUTCDate(startYesterday.getUTCDate() - 1);

    const [funnelToday, funnelYesterday, revToday, revYesterday] = await Promise.all([
      getFunnel(startToday.toISOString(), now.toISOString()),
      getFunnel(startYesterday.toISOString(), startToday.toISOString()),
      getRevenue(startToday.toISOString(), now.toISOString()),
      getRevenue(startYesterday.toISOString(), startToday.toISOString()),
    ]);

    // Top 3 new insights from last 24h
    const since24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const { data: insights } = await supabase
      .from("ai_insights")
      .select("title, description, expected_impact, confidence, insight_type")
      .eq("status", "new")
      .gte("created_at", since24h)
      .order("confidence", { ascending: false })
      .limit(3);

    const cvrToday = funnelToday.product_viewed > 0
      ? ((funnelToday.purchase_completed / funnelToday.product_viewed) * 100).toFixed(2)
      : "0.00";
    const cvrYesterday = funnelYesterday.product_viewed > 0
      ? ((funnelYesterday.purchase_completed / funnelYesterday.product_viewed) * 100).toFixed(2)
      : "0.00";
    const aovToday = revToday.orders > 0 ? Math.round(revToday.revenue / revToday.orders) : 0;

    let msg = `🤖 <b>ACOS Daily Digest</b>\n${now.toLocaleDateString("uk-UA")}\n\n`;
    msg += `<b>💰 Виторг:</b> ${fmtNum(revToday.revenue)} ₴ ${deltaPct(revToday.revenue, revYesterday.revenue)}\n`;
    msg += `<b>🛍 Замовлення:</b> ${revToday.orders} ${deltaPct(revToday.orders, revYesterday.orders)}\n`;
    msg += `<b>💳 AOV:</b> ${fmtNum(aovToday)} ₴\n`;
    msg += `<b>📊 CVR:</b> ${cvrToday}% (вчора ${cvrYesterday}%)\n\n`;

    msg += `<b>🔻 Воронка:</b>\n`;
    msg += `👁 PDP: ${fmtNum(funnelToday.product_viewed)} ${deltaPct(funnelToday.product_viewed, funnelYesterday.product_viewed)}\n`;
    msg += `🛒 Cart: ${fmtNum(funnelToday.add_to_cart)} ${deltaPct(funnelToday.add_to_cart, funnelYesterday.add_to_cart)}\n`;
    msg += `💳 Checkout: ${fmtNum(funnelToday.begin_checkout)} ${deltaPct(funnelToday.begin_checkout, funnelYesterday.begin_checkout)}\n`;
    msg += `✅ Purchase: ${fmtNum(funnelToday.purchase_completed)} ${deltaPct(funnelToday.purchase_completed, funnelYesterday.purchase_completed)}\n`;

    if (insights && insights.length > 0) {
      msg += `\n<b>💡 Топ інсайти (24г):</b>\n`;
      insights.forEach((ins, i) => {
        const aiTag = ins.insight_type.startsWith("ai_") ? "✨" : "📋";
        msg += `\n${i + 1}. ${aiTag} <b>${ins.title}</b>\n`;
        msg += `<i>${ins.description.slice(0, 200)}${ins.description.length > 200 ? "…" : ""}</i>\n`;
        if (ins.expected_impact) msg += `🎯 ${ins.expected_impact}\n`;
      });
    } else {
      msg += `\n<i>💤 Нових інсайтів за 24г немає.</i>\n`;
    }

    msg += `\n🔗 <a href="https://basic-food.shop/admin/insights">Відкрити панель</a>`;

    // Find admin/moderator chat IDs
    const { data: roleRows } = await supabase
      .from("user_roles")
      .select("user_id")
      .in("role", ["admin", "moderator"]);
    const adminUserIds = (roleRows ?? []).map((r) => r.user_id);

    if (adminUserIds.length === 0) {
      return {
        result: new Response(JSON.stringify({ sent: 0, reason: "no admins" }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }),
        summary: "No admins found",
        payload: { sent: 0, reason: "no_admins" },
      };
    }

    const { data: chats } = await supabase
      .from("telegram_chat_ids")
      .select("chat_id, user_id")
      .in("user_id", adminUserIds);

    const uniqueChatIds = Array.from(new Set((chats ?? []).map((c) => Number(c.chat_id))));
    const sendResults = await Promise.all(uniqueChatIds.map((cid) => sendTelegram(cid, msg)));
    const sent = sendResults.filter(Boolean).length;

    return {
      result: new Response(JSON.stringify({ sent, total: uniqueChatIds.length }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }),
      summary: `Sent digest to ${sent}/${uniqueChatIds.length} admin chats; revenue ${revToday.revenue}₴ (${revToday.orders} orders)`,
      payload: { sent, total: uniqueChatIds.length, revenue: revToday.revenue, orders: revToday.orders, cvr: cvrToday },
    };
    });
  } catch (err) {
    console.error("acos-daily-digest error", err);
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
