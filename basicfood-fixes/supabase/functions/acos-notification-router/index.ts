// ACOS Smart Notification Router — routes new ai_insights to appropriate channels
// with severity-based throttling and channel deduplication.
//
// Channels:
//   - telegram: HTML message to all admin chats
//   - inapp: notifications table (visible in admin bell)
//
// Routing rules (by risk_level):
//   - high   → telegram + inapp (immediate)
//   - medium → inapp only (immediate); telegram only if 3+ medium in 1h
//   - low    → inapp only (batched, no telegram)
//
// Anti-spam:
//   - Per insight_type Telegram throttle: max 1 message per 30 min
//   - Insights are marked status='routed' after dispatch to avoid re-processing
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";
const TG_THROTTLE_MIN = 30;

async function sendTelegram(chatId: number, text: string) {
  const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
  const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY");
  if (!LOVABLE_API_KEY || !TELEGRAM_API_KEY) return false;
  try {
    const res = await fetch(`${GATEWAY_URL}/sendMessage`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${LOVABLE_API_KEY}`,
        "X-Connection-Api-Key": TELEGRAM_API_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

function severityEmoji(risk: string) {
  if (risk === "high") return "🚨";
  if (risk === "medium") return "⚠️";
  return "ℹ️";
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-notification-router", req, null, async () => {
    const __res = await (async () => {
  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // Pull unrouted insights from the last 24h
    const since = new Date(Date.now() - 24 * 3600_000).toISOString();
    const { data: insights, error } = await supabase
      .from("ai_insights")
      .select("id, insight_type, title, description, risk_level, affected_layer, metrics, created_at")
      .eq("status", "new")
      .gte("created_at", since)
      .order("created_at", { ascending: true })
      .limit(50);
    if (error) throw error;
    if (!insights || insights.length === 0) {
      return new Response(JSON.stringify({ routed: 0, reason: "no_new_insights" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Get admin user_ids + chat_ids once
    const { data: admins } = await supabase.from("user_roles").select("user_id").eq("role", "admin");
    const adminIds = (admins ?? []).map((a) => a.user_id);
    const { data: chats } = adminIds.length > 0
      ? await supabase.from("telegram_chat_ids").select("chat_id, user_id").in("user_id", adminIds)
      : { data: [] };

    // Telegram throttle map: insight_type → last sent timestamp
    const throttleSince = new Date(Date.now() - TG_THROTTLE_MIN * 60_000).toISOString();
    const { data: recentRouted } = await supabase
      .from("ai_insights")
      .select("insight_type, metrics")
      .gte("updated_at", throttleSince)
      .eq("status", "routed");
    const lastTgByType = new Set<string>();
    for (const r of recentRouted ?? []) {
      if ((r.metrics as any)?.routed_telegram) lastTgByType.add(r.insight_type);
    }

    // Count medium-severity by type in last hour for escalation
    const hourAgo = new Date(Date.now() - 3600_000).toISOString();
    const { data: mediumLastHour } = await supabase
      .from("ai_insights")
      .select("insight_type")
      .gte("created_at", hourAgo)
      .eq("risk_level", "medium");
    const mediumCount = new Map<string, number>();
    for (const m of mediumLastHour ?? []) {
      mediumCount.set(m.insight_type, (mediumCount.get(m.insight_type) ?? 0) + 1);
    }

    let tgSent = 0;
    let inappSent = 0;
    let routedCount = 0;

    for (const ins of insights) {
      const risk = ins.risk_level ?? "low";
      let routeTg = false;
      const routeInapp = risk !== "low" || (ins.insight_type as string).startsWith("anomaly_");

      if (risk === "high") routeTg = true;
      else if (risk === "medium" && (mediumCount.get(ins.insight_type) ?? 0) >= 3) routeTg = true;

      // Apply throttle
      if (routeTg && lastTgByType.has(ins.insight_type)) routeTg = false;

      // Dispatch in-app notifications
      if (routeInapp && adminIds.length > 0) {
        const rows = adminIds.map((uid) => ({
          user_id: uid,
          type: "acos_insight",
          title: `${severityEmoji(risk)} ${ins.title}`,
          message: ins.description?.slice(0, 280) ?? null,
          reference_id: ins.id,
        }));
        // notifications has RLS requiring admin/moderator auth.uid(); service role bypasses.
        const { error: nErr } = await supabase.from("notifications").insert(rows as never);
        if (!nErr) inappSent += rows.length;
      }

      // Dispatch Telegram
      if (routeTg && (chats?.length ?? 0) > 0) {
        const text = `${severityEmoji(risk)} <b>${ins.title}</b>\n\n${ins.description}\n\n<i>Layer: ${ins.affected_layer ?? "—"} · Risk: ${risk}</i>\n\n👉 /admin/insights`;
        const results = await Promise.all(chats!.map((c) => sendTelegram(Number(c.chat_id), text)));
        tgSent += results.filter(Boolean).length;
        lastTgByType.add(ins.insight_type); // prevent same-batch duplicates
      }

      // Mark insight as routed
      await supabase
        .from("ai_insights")
        .update({
          status: "routed",
          metrics: {
            ...((ins.metrics as Record<string, unknown>) ?? {}),
            routed_at: new Date().toISOString(),
            routed_telegram: routeTg,
            routed_inapp: routeInapp,
          },
        })
        .eq("id", ins.id);
      routedCount++;
    }

    return new Response(
      JSON.stringify({
        routed: routedCount,
        telegram_sent: tgSent,
        inapp_sent: inappSent,
        admins: adminIds.length,
        chats: chats?.length ?? 0,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
