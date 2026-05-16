import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

async function alertAdmins(supabase: any, text: string) {
  const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
  const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY");
  if (!LOVABLE_API_KEY || !TELEGRAM_API_KEY) return 0;

  const { data: admins } = await supabase.from("user_roles").select("user_id").eq("role", "admin");
  const ids = (admins ?? []).map((a: any) => a.user_id);
  if (ids.length === 0) return 0;
  const { data: chats } = await supabase.from("telegram_chat_ids").select("chat_id").in("user_id", ids);

  const results = await Promise.all(
    (chats ?? []).map(async (c: any) => {
      try {
        await fetch(`${GATEWAY_URL}/sendMessage`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${LOVABLE_API_KEY}`,
            "X-Connection-Api-Key": TELEGRAM_API_KEY,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ chat_id: Number(c.chat_id), text, parse_mode: "HTML" }),
        });
        return 1;
      } catch (_) { return 0; }
    }),
  );
  return results.reduce((s, n) => s + n, 0);
}

async function countEvent(supabase: any, type: string, fromIso: string, toIso: string) {
  const { count } = await supabase
    .from("events")
    .select("*", { count: "exact", head: true })
    .eq("event_type", type)
    .gte("created_at", fromIso)
    .lt("created_at", toIso);
  return count ?? 0;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-anomaly-detector", req);
  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const now = Date.now();
    const t24 = new Date(now - 24 * 3600_000).toISOString();
    const t48 = new Date(now - 48 * 3600_000).toISOString();
    const t7d = new Date(now - 7 * 86400_000).toISOString();
    const t14d = new Date(now - 14 * 86400_000).toISOString();
    const nowIso = new Date(now).toISOString();

    const anomalies: { type: string; severity: "high" | "medium"; message: string }[] = [];

    // 1) Conversion drop: PDP→purchase ratio (24h vs prior 24h)
    const [pdp24, buy24, pdp48, buy48] = await Promise.all([
      countEvent(supabase, "product_viewed", t24, nowIso),
      countEvent(supabase, "purchase_completed", t24, nowIso),
      countEvent(supabase, "product_viewed", t48, t24),
      countEvent(supabase, "purchase_completed", t48, t24),
    ]);
    if (pdp24 >= 30 && pdp48 >= 30) {
      const cr24 = buy24 / pdp24;
      const cr48 = buy48 / pdp48;
      if (cr48 > 0 && cr24 < cr48 * 0.5) {
        anomalies.push({
          type: "conversion_drop",
          severity: "high",
          message: `Конверсія впала на ${Math.round((1 - cr24 / cr48) * 100)}% (з ${(cr48 * 100).toFixed(1)}% до ${(cr24 * 100).toFixed(1)}%)`,
        });
      }
    }

    // 2) Cart abandonment spike (24h vs 7d avg)
    const [cart24, cart7d] = await Promise.all([
      countEvent(supabase, "add_to_cart", t24, nowIso),
      countEvent(supabase, "add_to_cart", t7d, nowIso),
    ]);
    const [chk24, chk7d] = await Promise.all([
      countEvent(supabase, "begin_checkout", t24, nowIso),
      countEvent(supabase, "begin_checkout", t7d, nowIso),
    ]);
    if (cart24 >= 20 && cart7d >= 50) {
      const abandon24 = cart24 > 0 ? 1 - chk24 / cart24 : 0;
      const abandonAvg = cart7d > 0 ? 1 - chk7d / cart7d : 0;
      if (abandon24 > 0.85 && abandon24 > abandonAvg * 1.2) {
        anomalies.push({
          type: "cart_abandonment_spike",
          severity: "high",
          message: `Покинуті кошики: ${Math.round(abandon24 * 100)}% за 24г (середнє: ${Math.round(abandonAvg * 100)}%)`,
        });
      }
    }

    // 3) Revenue dip (last 24h vs prior 7d daily avg)
    const { data: orders24 } = await supabase
      .from("orders")
      .select("total")
      .gte("created_at", t24)
      .in("status", ["new", "processing", "shipped", "delivered", "completed"]);
    const { data: orders14d } = await supabase
      .from("orders")
      .select("total, created_at")
      .gte("created_at", t14d)
      .lt("created_at", t24)
      .in("status", ["new", "processing", "shipped", "delivered", "completed"]);
    const rev24 = (orders24 ?? []).reduce((s, o) => s + (o.total ?? 0), 0);
    const rev13Days = (orders14d ?? []).reduce((s, o) => s + (o.total ?? 0), 0);
    const dailyAvg = rev13Days / 13;
    if (dailyAvg >= 500 && rev24 < dailyAvg * 0.4) {
      anomalies.push({
        type: "revenue_dip",
        severity: "high",
        message: `Виторг за 24г: ${rev24.toLocaleString("uk-UA")}₴ (середнє ${Math.round(dailyAvg).toLocaleString("uk-UA")}₴/день, -${Math.round((1 - rev24 / dailyAvg) * 100)}%)`,
      });
    }

    // 4) Search-failure surge
    const { data: searches24 } = await supabase
      .from("events")
      .select("metadata")
      .eq("event_type", "search_performed")
      .gte("created_at", t24);
    if ((searches24?.length ?? 0) >= 20) {
      const zero = (searches24 ?? []).filter((s: any) => (s.metadata?.results_count ?? 0) === 0).length;
      const ratio = zero / (searches24?.length ?? 1);
      if (ratio > 0.5) {
        anomalies.push({
          type: "search_failure_surge",
          severity: "medium",
          message: `${Math.round(ratio * 100)}% пошуків (${zero}/${searches24?.length}) повертають 0 результатів`,
        });
      }
    }

    // 5) Bot-traffic anomaly (very high event rate from a single session)
    const { data: topSessions } = await supabase
      .from("events")
      .select("session_id")
      .gte("created_at", t24)
      .not("session_id", "is", null)
      .limit(5000);
    const sessMap = new Map<string, number>();
    for (const e of topSessions ?? []) {
      const k = e.session_id as string;
      sessMap.set(k, (sessMap.get(k) ?? 0) + 1);
    }
    const suspicious = [...sessMap.entries()].filter(([, c]) => c > 500);
    if (suspicious.length > 0) {
      anomalies.push({
        type: "bot_traffic",
        severity: "medium",
        message: `Підозріла активність: ${suspicious.length} сесій з 500+ подіями за 24г`,
      });
    }

    // Persist + alert (batch insert all anomalies at once)
    if (anomalies.length > 0) {
      await supabase.from("ai_insights").insert(
        anomalies.map((a) => ({
          insight_type: `anomaly_${a.type}`,
          title: `🚨 Аномалія: ${a.type.replace(/_/g, " ")}`,
          description: a.message,
          confidence: 0.85,
          affected_layer: "monitoring",
          risk_level: a.severity,
          status: "new",
          metrics: { detected_at: nowIso, ...a },
        })),
      ).catch(() => {});
    }

    let alertedAdmins = 0;
    const high = anomalies.filter((a) => a.severity === "high");
    if (high.length > 0) {
      const txt = `🚨 <b>ANOMALY DETECTED</b>\n\n${high.map((a) => `• ${a.message}`).join("\n")}\n\nДеталі: /admin/insights`;
      alertedAdmins = await alertAdmins(supabase, txt);
    }

    __agent.success();
    return new Response(
      JSON.stringify({ anomalies: anomalies.length, high: high.length, alerted_admins: alertedAdmins, details: anomalies }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    __agent.error(err);
        return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
