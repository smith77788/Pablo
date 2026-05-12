/**
 * Pablo Executive Brain — Claude Opus 4.7 powered strategic AI layer.
 *
 * Routes requests to the appropriate executive agent:
 *   - CEO: strategic decisions, prioritization
 *   - CMO: marketing strategy, campaigns
 *   - CFO: finance, unit economics
 *   - COO: operations, logistics, Nova Poshta
 *   - CoS: task routing, briefings
 *   - Analyst: KPI analysis, reports
 *
 * Request body:
 *   { agent: AgentRole, task: string, context?: Record<string, unknown> }
 *
 * Integrates with existing ACOS infrastructure:
 *   - reads ai_insights, agent_runs, customers, orders, products
 *   - writes pablo_executive_decisions, pablo_approval_queue, agent_action_log
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

type AgentRole = "ceo" | "cmo" | "cfo" | "coo" | "cos" | "analyst";

// ─── Agent System Prompts ─────────────────────────────────────────────────

const AGENT_PROMPTS: Record<AgentRole, string> = {
  ceo: `Ти — CEO BASIC.FOOD, Ukrainian D2C бренду натуральних повітряно-сушених ласощів для собак і котів.

БІЗНЕС-КОНТЕКСТ:
- Продаєм через сайт (React + Supabase) і Telegram бот
- Доставка через Нову Пошту по Україні
- Оплата: Monobank, WayForPay, накладений платіж
- Є B2C (роздріб) і B2B (wholesale) канали
- Ціни зберігаються в КОПІЙКАХ (÷100 = гривні)
- 100+ автоматизованих ACOS-агентів вже працюють

ТВОЇ ОБОВ'ЯЗКИ:
1. Стратегічне планування (квартал, рік)
2. Пріоритизація завдань (max 3 дії на день)
3. Затвердження ключових рішень
4. Оцінка ризиків
5. Ефективне використання ресурсів

ФОРМАТ ВІДПОВІДІ:
- Коротко, чітко, по суті
- Конкретні дії з дедлайнами
- Чіткий рівень ризику: LOW / MEDIUM / HIGH
- Завжди вказуй очікуваний вплив на виручку або інший KPI`,

  cmo: `Ти — CMO BASIC.FOOD, Marketing Director Ukrainian pet treats D2C brand.

МАРКЕТИНГОВА СТРАТЕГІЯ:
- Telegram-first (основний канал, бот інтегрований)
- Instagram/Facebook — orgаnic + paid
- Nova Poshta delivery — логістика впливає на маркетинг (% відмов = churn)
- Реферальна програма активна
- Lifecycle автоматизації: win-back, reorder reminders, CSAT

КЛЮЧОВІ МЕТРИКИ:
- CAC (вартість залучення), LTV, ROAS
- Конверсія воронки: PDP → Cart → Checkout → Purchase
- Open rate Telegram broadcasts
- % повторних покупок (retention)

ЗАВДАННЯ:
1. Оцінка ефективності кампаній
2. Рекомендації для broadcasts (Telegram)
3. Контент-стратегія (блог, SEO, Instagram)
4. Управління lifecycle клієнтів
5. A/B тести (ціна, повідомлення, landing pages)`,

  cfo: `Ти — CFO BASIC.FOOD, Chief Financial Officer.

ФІНАНСОВІ МЕТРИКИ (все в гривнях, UAH):
- Виручка (revenue = SUM orders.total WHERE status != 'cancelled')
- Маржа (gross margin = (price - cost) / price * 100%)
- CAC (cost per acquired customer)
- LTV (lifetime value = avg order value × avg order frequency × avg lifespan)
- Contribution Margin = Revenue - COGS - Variable Costs
- ROAS = Revenue / Ad Spend

УВАГА: всі суми в Supabase зберігаються в КОПІЙКАХ. Ділити на 100 для гривень.

ОБОВ'ЯЗКИ:
1. Аналіз прибутковості по продуктах і каналах
2. Контроль unit economics
3. Бюджетні рекомендації
4. Податкова оптимізація (FOP система)
5. Прогноз cashflow`,

  coo: `Ти — COO BASIC.FOOD, Chief Operating Officer.

ОПЕРАЦІЙНИЙ КОНТЕКСТ:
- Nova Poshta — єдиний перевізник
- % відмов (refused) = критичний показник (> 15% = alarm)
- Fulfillment: зберігання → пакування → відправка → трекінг
- Запаси: stock_quantity в таблиці products
- Низький запас: threshold 10 units
- Постачальники: Ukrainian producers

КЛЮЧОВІ ЗАВДАННЯ:
1. Моніторинг відправлень (tracking_number, status)
2. Аналіз відмов по містах і відділеннях
3. Управління запасами (restock alerts)
4. Оптимізація packaging і fulfillment
5. SLA на обробку замовлень (new → shipped за 24h)`,

  cos: `Ти — Chief of Staff у BASIC.FOOD. Твоя роль — координація між засновником і спеціалізованими агентами.

ОБОВ'ЯЗКИ:
1. Роутинг завдань до правильного агента (CEO/CMO/CFO/COO/Analyst)
2. Синтез інформації з кількох джерел
3. Підготовка ранкових брифінгів
4. Відстеження виконання рішень
5. Ескалація критичних питань

ПРИНЦИПИ:
- Фільтруй шум від сигналу (засновник бачить тільки важливе)
- Стисло і по суті (max 3 пункти на тему)
- Завжди вказуй терміновість: 🔴 Терміново / 🟡 Важливо / 🟢 Можна почекати`,

  analyst: `Ти — Business Analyst BASIC.FOOD. Аналізуєш дані та надаєш структуровані звіти.

МЕТОДОЛОГІЯ:
- Факти → інтерпретація → рекомендація
- Порівняння з попереднім періодом (WoW, MoM, YoY)
- Виявлення аномалій і трендів
- Статистична значимість (не роби висновки з малих вибірок)

ФОРМАТ ЗВІТУ:
## Ключові метрики
[таблиця з актуальними vs попередній період]

## Тренди
[що зростає / падає / стабільне]

## Аномалії
[що виходить за межі норми]

## Рекомендації
[конкретні дії з очікуваним ефектом]

Усі суми в гривнях (не копійках). Формат: 12 345 ₴`,
};

// ─── Business Context Fetcher ────────────────────────────────────────────

async function fetchBusinessContext(): Promise<Record<string, unknown>> {
  const [ordersResult, insightsResult, productsResult, customersResult] = await Promise.all([
    // Last 7 days orders summary
    supabase.from("orders")
      .select("status, total, created_at, source")
      .gte("created_at", new Date(Date.now() - 7 * 86400000).toISOString())
      .neq("status", "cancelled"),

    // Latest AI insights (ACOS)
    supabase.from("ai_insights")
      .select("insight_type, title, description, confidence, risk_level, expected_impact")
      .eq("status", "new")
      .order("created_at", { ascending: false })
      .limit(10),

    // Low stock products
    supabase.from("products")
      .select("name, stock_quantity, price")
      .eq("is_active", true)
      .lte("stock_quantity", 15)
      .order("stock_quantity"),

    // Customer stats
    supabase.from("customers")
      .select("lifecycle_stage, total_spent, total_orders")
      .limit(1000),
  ]);

  const orders = ordersResult.data || [];
  const insights = insightsResult.data || [];
  const lowStock = productsResult.data || [];
  const customers = customersResult.data || [];

  const revenue7d = orders.reduce((s, o) => s + (o.total || 0), 0);
  const orderCount7d = orders.length;

  // Lifecycle breakdown
  const lifecycle: Record<string, number> = {};
  for (const c of customers) {
    const stage = c.lifecycle_stage || "unknown";
    lifecycle[stage] = (lifecycle[stage] || 0) + 1;
  }

  return {
    period: "last_7_days",
    revenue_7d_uah: Math.round(revenue7d / 100),
    orders_7d: orderCount7d,
    avg_order_uah: orderCount7d > 0 ? Math.round(revenue7d / orderCount7d / 100) : 0,
    low_stock_products: lowStock.map(p => ({
      name: p.name,
      stock: p.stock_quantity,
      price_uah: Math.round((p.price || 0) / 100),
    })),
    pending_acos_insights: insights.length,
    top_insights: insights.slice(0, 5).map(i => ({
      type: i.insight_type,
      title: i.title,
      confidence: i.confidence,
      risk: i.risk_level,
      impact: i.expected_impact,
    })),
    customer_lifecycle: lifecycle,
    total_customers: customers.length,
  };
}

// ─── Decision Saver ───────────────────────────────────────────────────────

async function saveDecision(
  agent: AgentRole,
  decisionType: string,
  title: string,
  summary: string,
  reasoning: string,
  riskLevel: string,
  actions: unknown[],
  metrics: Record<string, unknown>,
): Promise<string> {
  const approvalStatus = riskLevel === "high"
    ? "pending"
    : riskLevel === "medium"
    ? "pending"
    : "auto_executed";

  const { data } = await supabase
    .from("pablo_executive_decisions")
    .insert({
      agent,
      decision_type: decisionType,
      title,
      summary,
      reasoning,
      risk_level: riskLevel,
      actions,
      approval_status: approvalStatus,
      metrics_snapshot: metrics,
      executed_at: approvalStatus === "auto_executed" ? new Date().toISOString() : null,
    })
    .select("id")
    .single();

  if (data?.id && approvalStatus === "pending") {
    await supabase.from("pablo_approval_queue").insert({
      decision_id: data.id,
      agent,
      action_type: decisionType,
      title,
      description: summary,
      risk_level: riskLevel,
      payload: { actions },
    });
  }

  // Log to existing agent_action_log
  await supabase.from("agent_action_log").insert({
    agent_type: `pablo-${agent}`,
    action_type: decisionType,
    decision: summary,
    payload: { agent, title, risk_level: riskLevel },
    status: "success",
    triggered_by: "pablo-executive-brain",
    severity: riskLevel === "high" ? "high" : riskLevel === "medium" ? "medium" : "low",
  }).single();

  return data?.id || "";
}

// ─── Main Handler ─────────────────────────────────────────────────────────

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const body = await req.json();
    const {
      agent = "cos",
      task,
      context = {},
      include_business_context = true,
    } = body as {
      agent: AgentRole;
      task: string;
      context?: Record<string, unknown>;
      include_business_context?: boolean;
    };

    if (!task) {
      return new Response(JSON.stringify({ error: "task is required" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const systemPrompt = AGENT_PROMPTS[agent] || AGENT_PROMPTS.cos;

    // Fetch live business data
    const businessData = include_business_context
      ? await fetchBusinessContext()
      : {};

    const contextBlock = Object.keys({ ...businessData, ...context }).length > 0
      ? `\n\n<business_context>\n${JSON.stringify({ ...businessData, ...context }, null, 2)}\n</business_context>`
      : "";

    // Call Claude Opus 4.7 with adaptive thinking
    const response = await anthropic.messages.create({
      model: "claude-opus-4-7",
      max_tokens: 4096,
      thinking: { type: "adaptive" },
      system: systemPrompt + contextBlock,
      messages: [{ role: "user", content: task }],
    });

    // Extract text from response
    const textBlocks = response.content.filter((b) => b.type === "text");
    const responseText = textBlocks.map((b) => (b as { type: "text"; text: string }).text).join("\n").trim();

    // Parse risk level from response (simple heuristic)
    const riskLevel = /HIGH RISK|КРИТИЧНИЙ РИЗИК|🔴/i.test(responseText)
      ? "high"
      : /MEDIUM RISK|СЕРЕДНІЙ РИЗИК|🟡/i.test(responseText)
      ? "medium"
      : "low";

    // Extract title (first line or first heading)
    const titleMatch = responseText.match(/^#+\s+(.+)|^([^\n]{5,80})/m);
    const title = titleMatch ? (titleMatch[1] || titleMatch[2] || task).slice(0, 200) : task.slice(0, 200);

    // Save decision
    const decisionId = await saveDecision(
      agent,
      "analysis",
      title,
      responseText.slice(0, 500),
      responseText,
      riskLevel,
      [],
      businessData,
    );

    return new Response(
      JSON.stringify({
        agent,
        decision_id: decisionId,
        response: responseText,
        risk_level: riskLevel,
        requires_approval: riskLevel !== "low",
        thinking_used: response.content.some((b) => b.type === "thinking"),
        input_tokens: response.usage.input_tokens,
        output_tokens: response.usage.output_tokens,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (error) {
    console.error("Pablo Executive Brain error:", error);
    return new Response(
      JSON.stringify({ error: (error as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
