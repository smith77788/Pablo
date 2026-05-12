/**
 * Pablo Morning Brief — щоденний ранковий брифінг від Claude.
 *
 * Запускається pg_cron о 06:00 UTC (09:00 Kyiv).
 * 1. Збирає ключові метрики за 24h та 7d
 * 2. Запитує Claude CEO agent (synthesis)
 * 3. Зберігає в pablo_briefings
 * 4. Надсилає в Telegram засновнику
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

interface OrderRow { total: number; status: string; source: string; created_at: string; }
interface ProductRow { name: string; stock_quantity: number; price: number; }
interface InsightRow { title: string; description: string; expected_impact: string | null; risk_level: string; }
interface CustomerRow { lifecycle_stage: string; total_orders: number; }

async function gatherMetrics() {
  const now = new Date();
  const since24h = new Date(now.getTime() - 86400000).toISOString();
  const since7d = new Date(now.getTime() - 7 * 86400000).toISOString();
  const since30d = new Date(now.getTime() - 30 * 86400000).toISOString();

  const [
    orders24h,
    orders7d,
    orders30d,
    lowStock,
    newInsights,
    customers,
    pendingApprovals,
  ] = await Promise.all([
    supabase.from("orders").select("total, status, source").gte("created_at", since24h).neq("status", "cancelled"),
    supabase.from("orders").select("total, status").gte("created_at", since7d).neq("status", "cancelled"),
    supabase.from("orders").select("total").gte("created_at", since30d).neq("status", "cancelled"),
    supabase.from("products").select("name, stock_quantity, price").eq("is_active", true).lte("stock_quantity", 10).order("stock_quantity"),
    supabase.from("ai_insights").select("title, description, expected_impact, risk_level").eq("status", "new").order("created_at", { ascending: false }).limit(8),
    supabase.from("customers").select("lifecycle_stage, total_orders").limit(2000),
    supabase.from("pablo_approval_queue").select("id").eq("status", "pending"),
  ]);

  const calc = (orders: OrderRow[]) => ({
    count: orders.length,
    revenue: orders.reduce((s, o) => s + (o.total || 0), 0),
  });

  const d24 = calc(orders24h.data || []);
  const d7 = calc(orders7d.data || []);
  const d30 = calc(orders30d.data || []);

  // Source breakdown
  const sources: Record<string, number> = {};
  for (const o of (orders24h.data || [])) {
    sources[o.source || "site"] = (sources[o.source || "site"] || 0) + 1;
  }

  // Lifecycle
  const lifecycle: Record<string, number> = {};
  for (const c of (customers.data || [])) {
    const s = c.lifecycle_stage || "unknown";
    lifecycle[s] = (lifecycle[s] || 0) + 1;
  }

  const repeatBuyers = (customers.data || []).filter(c => c.total_orders > 1).length;
  const totalCustomers = (customers.data || []).length;

  return {
    today: { orders: d24.count, revenue_uah: Math.round(d24.revenue / 100) },
    week: { orders: d7.count, revenue_uah: Math.round(d7.revenue / 100) },
    month: { orders: d30.count, revenue_uah: Math.round(d30.revenue / 100) },
    avg_order_today_uah: d24.count > 0 ? Math.round(d24.revenue / d24.count / 100) : 0,
    sources_24h: sources,
    low_stock: (lowStock.data || []).map((p: ProductRow) => `${p.name}: ${p.stock_quantity} шт`),
    acos_insights: (newInsights.data || []).map((i: InsightRow) => ({
      title: i.title,
      impact: i.expected_impact,
      risk: i.risk_level,
    })),
    customer_stats: {
      total: totalCustomers,
      repeat_rate_pct: totalCustomers > 0 ? Math.round(repeatBuyers / totalCustomers * 100) : 0,
      lifecycle,
    },
    pending_pablo_approvals: (pendingApprovals.data || []).length,
  };
}

function formatTelegramMessage(briefing: string, metrics: ReturnType<typeof gatherMetrics> extends Promise<infer T> ? T : never): string {
  const rev = metrics.today.revenue_uah.toLocaleString("uk-UA");
  const orders = metrics.today.orders;
  const lowStockAlert = metrics.low_stock.length > 0
    ? `\n⚠️ <b>Низький запас:</b> ${metrics.low_stock.slice(0, 3).join(", ")}`
    : "";
  const approvalsAlert = metrics.pending_pablo_approvals > 0
    ? `\n🔔 <b>Очікують вашого підтвердження:</b> ${metrics.pending_pablo_approvals} рішень`
    : "";

  return `🌅 <b>Ранковий брифінг BASIC.FOOD</b>

📦 <b>За 24 години:</b> ${orders} замовлень | ${rev} ₴
📊 <b>Тиждень:</b> ${metrics.week.orders} замовлень | ${metrics.week.revenue_uah.toLocaleString("uk-UA")} ₴${lowStockAlert}${approvalsAlert}

${briefing.slice(0, 2000)}

<a href="https://basic-food.shop/admin/pablo-ai">→ Відкрити Pablo AI</a>`;
}

async function sendTelegram(chatId: number, text: string) {
  if (!TG_TOKEN) return false;
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  try {
    const metrics = await gatherMetrics();

    // Ask Claude CEO to synthesize the morning brief
    const ceoPrompt = `Підготуй РАНКОВИЙ БРИФІНГ для засновника BASIC.FOOD.

Дані за останні 24 години:
${JSON.stringify(metrics, null, 2)}

ФОРМАТ БРИФІНГУ:
1. **Стан бізнесу** (1 речення — добре/погано/нормально)
2. **Головне за вчора** (2-3 пункти)
3. **Тривоги** (якщо є: низький запас, падіння продажів, аномалії)
4. **Топ-3 дії на сьогодні** (конкретні, з очікуваним результатом)
5. **Підказки від ACOS** (що радить система, 2-3 найважливіших)

Будь прямим і конкретним. Ніяких загальних слів. Максимум 300 слів.`;

    const response = await anthropic.messages.create({
      model: "claude-opus-4-7",
      max_tokens: 1500,
      thinking: { type: "adaptive" },
      system: `Ти — CEO BASIC.FOOD, Ukrainian D2C pet treats brand. Щоранку готуєш стислий брифінг для себе (засновника).
Стиль: як мислить CEO — факти, цифри, конкретні дії. Жодної води.
Мова: УКРАЇНСЬКА. Всі суми в гривнях (UAH).`,
      messages: [{ role: "user", content: ceoPrompt }],
    });

    const briefingText = response.content
      .filter(b => b.type === "text")
      .map(b => (b as { type: "text"; text: string }).text)
      .join("\n")
      .trim();

    // Save briefing
    const { data: briefingRow } = await supabase
      .from("pablo_briefings")
      .insert({
        briefing_type: "morning",
        title: `Ранковий брифінг ${new Date().toLocaleDateString("uk-UA")}`,
        content: briefingText,
        metrics,
        top_actions: [],
      })
      .select("id")
      .single();

    // Get admin Telegram chat IDs
    const { data: adminChats } = await supabase
      .from("bot_settings")
      .select("admin_chat_id")
      .not("admin_chat_id", "is", null)
      .limit(5);

    const chatIds = (adminChats || []).map((r: { admin_chat_id: number }) => r.admin_chat_id).filter(Boolean);

    // Also check ai_memory for founder's chat_id
    const { data: founderMemory } = await supabase
      .from("ai_memory")
      .select("learned_rule")
      .eq("pattern_key", "founder_telegram_chat_id")
      .single();

    if (founderMemory?.learned_rule) {
      const founderId = parseInt(founderMemory.learned_rule);
      if (!isNaN(founderId) && !chatIds.includes(founderId)) {
        chatIds.push(founderId);
      }
    }

    // Send to Telegram
    const sentTo: number[] = [];
    const tgMessage = formatTelegramMessage(briefingText, metrics);

    for (const chatId of chatIds) {
      const sent = await sendTelegram(chatId, tgMessage);
      if (sent) sentTo.push(chatId);
    }

    // Update briefing with sent status
    if (briefingRow?.id) {
      await supabase
        .from("pablo_briefings")
        .update({ sent_to_tg: sentTo.length > 0, tg_chat_ids: sentTo })
        .eq("id", briefingRow.id);
    }

    // Save to ai_insights for visibility in existing ACOS dashboard
    await supabase.from("ai_insights").insert({
      insight_type: "pablo_morning_brief",
      title: `Ранковий брифінг Pablo AI — ${new Date().toLocaleDateString("uk-UA")}`,
      description: briefingText.slice(0, 500),
      confidence: 0.95,
      status: "new",
      risk_level: metrics.low_stock.length > 0 ? "medium" : "low",
      affected_layer: "executive",
      expected_impact: `Замовлень сьогодні: ${metrics.today.orders}, виручка: ${metrics.today.revenue_uah} ₴`,
    });

    return new Response(
      JSON.stringify({
        ok: true,
        briefing_id: briefingRow?.id,
        sent_to_tg: sentTo.length,
        metrics: { orders_24h: metrics.today.orders, revenue_24h_uah: metrics.today.revenue_uah },
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error("Pablo Morning Brief error:", err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
