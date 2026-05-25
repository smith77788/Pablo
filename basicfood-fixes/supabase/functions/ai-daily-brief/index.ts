/**
 * 📰 AI Daily Brief
 *
 * Запуск: cron щодня о 08:00 (Europe/Kyiv).
 *
 * Що робить:
 *  1. Збирає метрики за останні 24h:
 *     - замовлення (count, revenue), AOV
 *     - top-3 продукти за продажами
 *     - tribunal активність (cases / verdicts breakdown)
 *     - outreach (posted vs failed)
 *     - відкриті debug_reports
 *  2. Викликає Lovable AI (google/gemini-2.5-flash) для генерації
 *     "ранкового брифінгу" українською (≤180 слів).
 *  3. Зберігає як ai_insights з insight_type='daily_brief'.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";
import { routeAI } from "../_shared/ai-router.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY")!;
const TELEGRAM_API_KEY_1 = Deno.env.get("TELEGRAM_API_KEY_1");
const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";
const OPEN_DEBUG_STATUSES = ["new", "triaged", "auto_fixing", "manual_required"] as const;

async function sendBriefToAdmins(sb: any, brief: string, m: MetricsBundle): Promise<{ sent: number; failed: number }> {
  if (!LOVABLE_API_KEY || !TELEGRAM_API_KEY_1) return { sent: 0, failed: 0 };
  const { data: roles } = await sb.from("user_roles").select("user_id").in("role", ["admin"]);
  const userIds = (roles ?? []).map((r: any) => r.user_id);
  if (userIds.length === 0) return { sent: 0, failed: 0 };
  const { data: chats } = await sb.from("telegram_chat_ids").select("chat_id").in("user_id", userIds);
  const list = (chats ?? []) as { chat_id: number }[];
  if (list.length === 0) return { sent: 0, failed: 0 };

  const escape = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const today = new Date().toISOString().slice(0, 10);
  const text =
    `📰 <b>Ранковий брифінг — ${today}</b>\n\n` +
    `${escape(brief)}\n\n` +
    `<b>📊 Швидкі цифри:</b>\n` +
    `• Замовлень: <b>${m.orders_24h}</b> | Виторг: <b>${m.revenue_24h}₴</b> | AOV: <b>${m.aov}₴</b>\n` +
    `• Outreach: ${m.outreach_posted_24h}✓ / ${m.outreach_failed_24h}✗\n` +
    `• Tribunal: ${m.tribunal_cases_24h} кейсів | Інциденти: ${m.open_incidents}` +
    (m.silent_agents > 0 ? ` (silent: ${m.silent_agents})` : "");

  const results = await Promise.all(list.map(async (c) => {
    try {
      const res = await fetch(`${GATEWAY_URL}/sendMessage`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${LOVABLE_API_KEY}`,
          "X-Connection-Api-Key": TELEGRAM_API_KEY_1,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ chat_id: c.chat_id, text, parse_mode: "HTML", disable_web_page_preview: true }),
      });
      return res.ok;
    } catch { return false; }
  }));
  const sent = results.filter(Boolean).length;
  const failed = results.length - sent;
  return { sent, failed };
}

interface MetricsBundle {
  orders_24h: number;
  revenue_24h: number;
  aov: number;
  top_products: { name: string; count: number }[];
  tribunal_cases_24h: number;
  tribunal_verdicts: Record<string, number>;
  outreach_posted_24h: number;
  outreach_failed_24h: number;
  open_incidents: number;
  silent_agents: number;
}

async function gatherMetrics(sb: any): Promise<MetricsBundle> {
  const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();

  const [orders, items, cases, verdicts, outreach, reports] = await Promise.all([
    sb.from("orders").select("total, status").gte("created_at", since),
    sb.from("order_items")
      .select("product_name, quantity, orders!inner(created_at)")
      .gte("orders.created_at", since),
    sb.from("tribunal_cases").select("id", { count: "exact", head: true }).gte("created_at", since),
    sb.from("tribunal_verdicts").select("verdict").gte("decided_at", since),
    sb.from("outreach_actions").select("status").gte("created_at", since),
    sb.from("debug_reports").select("id, fingerprint", { count: "exact" }).in("status", [...OPEN_DEBUG_STATUSES]).is("resolved_at", null).limit(500),
  ]);

  const orderRows = (orders.data ?? []) as { total: number; status: string }[];
  const validOrders = orderRows.filter((o) => o.status !== "cancelled");
  const revenue = validOrders.reduce((s, o) => s + (o.total ?? 0), 0);
  const orderCount = validOrders.length;
  const aov = orderCount > 0 ? Math.round(revenue / orderCount) : 0;

  const productMap = new Map<string, number>();
  for (const it of (items.data ?? []) as any[]) {
    const name = (it.product_name ?? "?").replace(/\s*\(опт\)\s*$/i, "");
    productMap.set(name, (productMap.get(name) ?? 0) + (it.quantity ?? 1));
  }
  const top_products = [...productMap.entries()]
    .sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([name, count]) => ({ name, count }));

  const verdictCounts: Record<string, number> = {};
  for (const v of (verdicts.data ?? []) as { verdict: string }[]) {
    verdictCounts[v.verdict] = (verdictCounts[v.verdict] ?? 0) + 1;
  }

  const outreachRows = (outreach.data ?? []) as { status: string }[];
  const posted = outreachRows.filter((r) => r.status === "posted").length;
  const failed = outreachRows.filter((r) => r.status === "failed").length;

  const reportRows = (reports.data ?? []) as { fingerprint: string }[];
  const silent = reportRows.filter((r) => (r.fingerprint ?? "").startsWith("agent-silence:")).length;

  return {
    orders_24h: orderCount,
    revenue_24h: revenue,
    aov,
    top_products,
    tribunal_cases_24h: cases.count ?? 0,
    tribunal_verdicts: verdictCounts,
    outreach_posted_24h: posted,
    outreach_failed_24h: failed,
    open_incidents: reports.count ?? 0,
    silent_agents: silent,
  };
}

async function generateBrief(m: MetricsBundle): Promise<string> {
  const top = m.top_products.map((p) => `${p.name}×${p.count}`).join(", ") || "—";
  const verdicts = Object.entries(m.tribunal_verdicts).map(([k, v]) => `${k}:${v}`).join(", ") || "—";
  const sys =
    "Ти — операційний помічник e-commerce компанії з кормами для тварин. Створи короткий ранковий брифінг для власника українською. Тон — дружній, конкретний, без води. Максимум 180 слів. Структура: 1) Підсумок продажів 2) Що робить AI-інфраструктура 3) Що потребує уваги. Без markdown, без bullets — звичний короткий текст.";
  const user = `Метрики за останні 24 години:
- Замовлень: ${m.orders_24h}, виторг: ${m.revenue_24h}₴, середній чек: ${m.aov}₴
- ТОП продукти: ${top}
- Tribunal: ${m.tribunal_cases_24h} кейсів, вердикти — ${verdicts}
- Outreach: ${m.outreach_posted_24h} опубліковано, ${m.outreach_failed_24h} провалено
- Відкриті інциденти: ${m.open_incidents} (з них silent agents: ${m.silent_agents})
Зроби живий брифінг.`;

  try {
    const result = await routeAI({
      model: "google/gemini-2.5-flash",
      messages: [
        { role: "system", content: sys },
        { role: "user", content: user },
      ],
      skipLovable: true,
      timeoutMs: 25_000,
    });
    return result.content ?? `Без LLM: 24h — ${m.orders_24h} замовлень / ${m.revenue_24h}₴, AOV ${m.aov}₴.`;
  } catch (e) {
    console.warn("[ai-daily-brief] AI router failed:", (e as Error).message);
    return `Без LLM: 24h — ${m.orders_24h} замовлень / ${m.revenue_24h}₴, AOV ${m.aov}₴. Outreach: ${m.outreach_posted_24h} posted / ${m.outreach_failed_24h} failed. Tribunal: ${m.tribunal_cases_24h} cases. Інциденти: ${m.open_incidents}.`;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;
  const body = await req.clone().json().catch(() => ({}));

  return await withAgentRun("ai-daily-brief", detectTrigger(req, body), async () => {
    const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
    const metrics = await gatherMetrics(sb);
    let brief: string;
    let llm_status: "ok" | "fallback" = "ok";
    try {
      brief = await generateBrief(metrics);
    } catch (e) {
      llm_status = "fallback";
      const top = metrics.top_products.map((p) => `${p.name}×${p.count}`).join(", ") || "—";
      brief = `Авто-фолбек (LLM недоступне: ${String((e as Error)?.message ?? e).slice(0, 80)}).\n\n` +
        `За останні 24 години: ${metrics.orders_24h} замовлень на ${metrics.revenue_24h}₴ (AOV ${metrics.aov}₴).\n` +
        `ТОП: ${top}.\n` +
        `Outreach: ${metrics.outreach_posted_24h} опубліковано / ${metrics.outreach_failed_24h} провалено.\n` +
        `Tribunal: ${metrics.tribunal_cases_24h} нових кейсів. Інциденти: ${metrics.open_incidents}.`;
    }

    // Зберегти як insight (один на день — дедуп через ai_insights_set_dedup_bucket НЕ
    // підходить, він hourly; робимо власну перевірку: якщо вже є daily_brief за сьогодні,
    // оновимо)
    const dayStart = new Date();
    dayStart.setUTCHours(0, 0, 0, 0);
    const { data: existing } = await sb.from("ai_insights")
      .select("id")
      .eq("insight_type", "daily_brief")
      .gte("created_at", dayStart.toISOString())
      .limit(1)
      .maybeSingle();

    const payload = {
      title: `Ранковий брифінг — ${new Date().toISOString().slice(0, 10)}`,
      description: brief,
      insight_type: "daily_brief",
      affected_layer: "ops",
      risk_level: "low",
      status: "new",
      confidence: 0.9,
      metrics: metrics as any,
    };

    const wasNewToday = !existing?.id;
    if (existing?.id) {
      await sb.from("ai_insights").update(payload).eq("id", existing.id);
    } else {
      await sb.from("ai_insights").insert(payload);
    }

    // Telegram delivery — раз на день автоматично, або примусово через POST { force_send: true }
    const forceSend = body?.force_send === true;
    let delivery = { sent: 0, failed: 0 };
    if (wasNewToday || forceSend) {
      delivery = await sendBriefToAdmins(sb, brief, metrics);
    }

    return {
      result: new Response(JSON.stringify({ ok: true, brief, metrics, llm_status, delivery }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }),
      summary: `orders=${metrics.orders_24h}, revenue=${metrics.revenue_24h}, incidents=${metrics.open_incidents}, llm=${llm_status}, tg=${delivery.sent}/${delivery.sent + delivery.failed}`,
      payload: { metrics_summary: { orders: metrics.orders_24h, revenue: metrics.revenue_24h }, llm_status, delivery },
      status: llm_status === "fallback" ? "partial" : "success",
    };
  }).catch((e) => {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  });
});
