/**
 * ACOS ROI Tracker
 *
 * Cron: щогодини.
 *
 * Що робить:
 *  1. Бере ai_actions у статусі 'applied', applied_at між 24h і 14d тому,
 *     measured_at IS NULL, reverted_at IS NULL.
 *  2. Для кожної дії — обчислює реальний impact:
 *      - якщо параметри містять product_id → порівнює виручку по цьому продукту
 *        за вікно (applied_at .. now()) проти вікна тієї ж тривалості перед applied_at
 *      - якщо параметри містять page_path → порівнює сесії/конверсії на цій сторінці
 *      - інакше: глобальна виручка (як грубий fallback)
 *  3. Записує actual_result.delta = (current - baseline) / max(baseline, 1)
 *     і ставить status='measured', measured_at=now().
 *  4. Створює insight 'roi_measured' якщо |delta| > 0.2 (помітний вплив).
 *
 * Це закриває цикл:
 *   Tribunal → Enforcer → ACTION → ROI Tracker → Consolidator → Tribunal стає розумнішим.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const MIN_AGE_HOURS = 24;
const MAX_AGE_DAYS = 14;
const NOTABLE_DELTA = 0.2; // ±20%

interface ActionRow {
  id: string;
  agent_id: string;
  action_type: string;
  applied_at: string;
  parameters: Record<string, unknown>;
  target_entity: string | null;
  target_id: string | null;
}

async function revenueForProduct(
  sb: any,
  productId: string,
  fromIso: string,
  toIso: string,
): Promise<number> {
  const { data: orders } = await sb
    .from("orders")
    .select("id")
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .neq("status", "cancelled")
    .limit(5000);
  const ids = (orders ?? []).map((o: any) => o.id as string);
  if (ids.length === 0) return 0;
  const chunkSize = 200;
  const chunks: string[][] = [];
  for (let i = 0; i < ids.length; i += chunkSize) chunks.push(ids.slice(i, i + chunkSize));
  const chunkResults = await Promise.all(
    chunks.map((slice) =>
      sb.from("order_items").select("product_price, quantity").eq("product_id", productId).in("order_id", slice)
    ),
  );
  let revenue = 0;
  for (const { data: items } of chunkResults) {
    for (const it of items ?? []) {
      revenue += ((it.product_price as number) || 0) * ((it.quantity as number) || 1);
    }
  }
  return revenue;
}

async function sessionsForPage(
  sb: any,
  pagePath: string,
  fromIso: string,
  toIso: string,
): Promise<number> {
  // BUGFIX: event_type у БД — 'page_view', не 'page_viewed'.
  // Поєднуємо обидва для зворотної сумісності зі старими записами.
  const { data } = await sb
    .from("events")
    .select("session_id")
    .in("event_type", ["page_view", "page_viewed"])
    .eq("url", pagePath)
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .limit(10000);
  const set = new Set<string>();
  for (const r of data ?? []) if (r.session_id) set.add(r.session_id as string);
  return set.size;
}

async function globalRevenue(
  sb: any,
  fromIso: string,
  toIso: string,
): Promise<number> {
  const { data } = await sb
    .from("orders")
    .select("total")
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .neq("status", "cancelled")
    .limit(5000);
  let r = 0;
  for (const o of data ?? []) r += (o.total as number) || 0;
  return r;
}

/**
 * Revenue from orders attributed to a specific source/channel
 * (broadcast, telegram_bot, promo, etc.) via orders.source field.
 */
async function revenueBySource(
  sb: any,
  source: string,
  fromIso: string,
  toIso: string,
): Promise<number> {
  const { data } = await sb
    .from("orders")
    .select("total")
    .eq("source", source)
    .gte("created_at", fromIso)
    .lte("created_at", toIso)
    .neq("status", "cancelled")
    .limit(5000);
  let r = 0;
  for (const o of data ?? []) r += (o.total as number) || 0;
  return r;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const now = Date.now();
    const oldest = new Date(now - MAX_AGE_DAYS * DAY_MS).toISOString();
    const newest = new Date(now - MIN_AGE_HOURS * HOUR_MS).toISOString();

    const { data: actions, error } = await sb
      .from("ai_actions")
      .select("id, agent_id, action_type, applied_at, parameters, target_entity, target_id")
      .eq("status", "applied")
      .is("measured_at", null)
      .is("reverted_at", null)
      .gte("applied_at", oldest)
      .lte("applied_at", newest)
      .limit(50);

    if (error) throw error;

    const results: Array<Record<string, unknown>> = [];
    let measuredCount = 0;
    let notableCount = 0;

    for (const act of (actions ?? []) as ActionRow[]) {
      const params = act.parameters ?? {};
      const productId = typeof params.product_id === "string"
        ? params.product_id
        : (act.target_entity === "product" ? act.target_id : null);
      const pagePath = typeof params.page_path === "string" ? params.page_path : null;
      const sourceChannel = typeof params.source === "string"
        ? params.source
        : (act.action_type === "broadcast" ? "telegram_bot"
          : act.action_type === "promo" ? "site"
          : null);

      const appliedAt = new Date(act.applied_at);
      const rawWindowMs = now - appliedAt.getTime();
      // FIX: для глобальних дій без таргетного scope використовуємо мінімум 7 днів
      // вікна, щоб уникнути baseline=current при низькому добовому трафіку.
      const isNarrowScope = !!productId || !!pagePath;
      const minWindowMs = isNarrowScope ? rawWindowMs : Math.max(rawWindowMs, 7 * DAY_MS);
      const windowMs = minWindowMs;
      const baselineFrom = new Date(appliedAt.getTime() - windowMs).toISOString();
      const baselineTo = act.applied_at;
      const currentFrom = act.applied_at;
      const currentTo = new Date(now).toISOString();

      let scope = "global";
      let baseline = 0;
      let current = 0;

      if (productId) {
        scope = `product:${productId}`;
        [baseline, current] = await Promise.all([
          revenueForProduct(sb, productId, baselineFrom, baselineTo),
          revenueForProduct(sb, productId, currentFrom, currentTo),
        ]);
      } else if (pagePath) {
        scope = `page:${pagePath}`;
        [baseline, current] = await Promise.all([
          sessionsForPage(sb, pagePath, baselineFrom, baselineTo),
          sessionsForPage(sb, pagePath, currentFrom, currentTo),
        ]);
      } else if (sourceChannel) {
        // FIX: broadcast/promo дії — вимірюємо лише атрибутований канал,
        // а не весь магазин. Інакше неможливо помітити вплив.
        scope = `source:${sourceChannel}`;
        [baseline, current] = await Promise.all([
          revenueBySource(sb, sourceChannel, baselineFrom, baselineTo),
          revenueBySource(sb, sourceChannel, currentFrom, currentTo),
        ]);
      } else {
        [baseline, current] = await Promise.all([
          globalRevenue(sb, baselineFrom, baselineTo),
          globalRevenue(sb, currentFrom, currentTo),
        ]);
      }

      const delta = baseline > 0 ? (current - baseline) / baseline : (current > 0 ? 1 : 0);

      await sb
        .from("ai_actions")
        .update({
          status: "measured",
          measured_at: new Date().toISOString(),
          actual_result: {
            scope,
            baseline,
            current,
            delta,
            window_hours: Math.round(windowMs / HOUR_MS),
            measured_by: "acos-roi-tracker",
            measured_at: new Date().toISOString(),
          },
        })
        .eq("id", act.id);

      measuredCount++;
      results.push({ id: act.id, scope, baseline, current, delta });

      if (Math.abs(delta) >= NOTABLE_DELTA) {
        notableCount++;
        const sign = delta > 0 ? "+" : "";
        await sb.from("ai_insights").insert({
          insight_type: "roi_measured",
          affected_layer: "learning",
          risk_level: delta < 0 ? "medium" : "low",
          title: `ROI: ${act.action_type} → ${sign}${(delta * 100).toFixed(1)}%`,
          description: `Дія "${act.action_type}" агента "${act.agent_id}" дала зміну ${sign}${(delta * 100).toFixed(1)}% (${scope}). Baseline=${baseline.toFixed(0)}, current=${current.toFixed(0)}. Consolidator врахує це у наступному циклі.`,
          confidence: 0.85,
          metrics: {
            action_id: act.id,
            agent_id: act.agent_id,
            action_type: act.action_type,
            scope,
            baseline,
            current,
            delta,
          },
          status: "new",
        });
      }
    }

    return new Response(
      JSON.stringify({
        ok: true,
        checked: actions?.length ?? 0,
        measured: measuredCount,
        notable: notableCount,
        results,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-roi-tracker error", err);
    return new Response(
      JSON.stringify({ ok: false, error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
