// ACOS Bundle Auto-Promote — finds the strongest co-purchase pair from the
// last 90 days. Instead of writing the promotion + promo_code directly, it
// enqueues a Tribunal case so prosecutor → judge → enforcer can vet the
// change. When called by the enforcer (`from_tribunal=true`) the function
// performs the real DB writes using the approved payload.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { enqueueTribunalCase } from "../_shared/tribunal.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const dayMs = 24 * 60 * 60 * 1000;
const LOOKBACK_DAYS = 90;
const MIN_CO_OCCURRENCE = 5;
const PROMO_TTL_DAYS = 30;
const DISCOUNT_PCT = 10;
const MIN_ORDER = 350;
const COOLDOWN_DAYS = 30;

interface OrderItemRow {
  order_id: string;
  product_id: string | null;
  product_name: string;
  product_price: number;
  quantity: number;
}

interface ProductRow {
  id: string;
  name: string;
  price: number;
  is_active: boolean;
  stock_quantity: number;
}

interface PairStat {
  a: string;
  b: string;
  a_name: string;
  b_name: string;
  count: number;
  combined_revenue: number;
}

const pairKey = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-bundle-auto-promote", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));

    // ── Branch B: enforcer is calling us back with an approved verdict ──
    if (body?.from_tribunal === true) {
      return await applyApprovedBundle(supabase, body);
    }

    const dryRun = body.dry_run === true;

    const now = Date.now();
    const since = new Date(now - LOOKBACK_DAYS * dayMs).toISOString();

    const { data: orders } = await supabase
      .from("orders")
      .select("id")
      .gte("created_at", since)
      .neq("source", "spin_game")
      .in("status", ["new", "confirmed", "shipped", "delivered", "paid"])
      .limit(5000);

    const orderIds = (orders ?? []).map((o) => o.id);
    if (orderIds.length === 0) {
      return new Response(
        JSON.stringify({ pairs: 0, reason: "no_orders" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const allItems: OrderItemRow[] = [];
    const chunkSize = 500;
    const itemChunks: string[][] = [];
    for (let i = 0; i < orderIds.length; i += chunkSize) itemChunks.push(orderIds.slice(i, i + chunkSize));
    const itemChunkResults = await Promise.all(
      itemChunks.map((chunk) =>
        supabase.from("order_items").select("order_id, product_id, product_name, product_price, quantity").in("order_id", chunk)
      ),
    );
    for (const { data: items } of itemChunkResults) {
      if (items) allItems.push(...(items as OrderItemRow[]));
    }

    const orderProducts = new Map<string, Map<string, OrderItemRow>>();
    for (const it of allItems) {
      if (!it.product_id) continue;
      let bucket = orderProducts.get(it.order_id);
      if (!bucket) {
        bucket = new Map();
        orderProducts.set(it.order_id, bucket);
      }
      bucket.set(it.product_id, it);
    }

    const pairCounts = new Map<string, PairStat>();
    for (const items of orderProducts.values()) {
      const ids = [...items.keys()];
      if (ids.length < 2) continue;
      for (let i = 0; i < ids.length; i++) {
        for (let j = i + 1; j < ids.length; j++) {
          const a = ids[i];
          const b = ids[j];
          const key = pairKey(a, b);
          let stat = pairCounts.get(key);
          if (!stat) {
            const itemA = items.get(a)!;
            const itemB = items.get(b)!;
            const [first, firstName, second, secondName] = a < b
              ? [a, itemA.product_name, b, itemB.product_name]
              : [b, itemB.product_name, a, itemA.product_name];
            stat = {
              a: first,
              b: second,
              a_name: firstName,
              b_name: secondName,
              count: 0,
              combined_revenue: 0,
            };
            pairCounts.set(key, stat);
          }
          stat.count++;
          const itemA = items.get(a)!;
          const itemB = items.get(b)!;
          stat.combined_revenue +=
            itemA.product_price * itemA.quantity +
            itemB.product_price * itemB.quantity;
        }
      }
    }

    const ranked = [...pairCounts.values()]
      .filter((p) => p.count >= MIN_CO_OCCURRENCE)
      .sort((a, b) => b.count - a.count || b.combined_revenue - a.combined_revenue);

    if (ranked.length === 0) {
      return new Response(
        JSON.stringify({ pairs: 0, reason: "no_qualifying_pairs", min_co_occurrence: MIN_CO_OCCURRENCE }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const topPair = ranked[0];
    const { data: prodRows } = await supabase
      .from("products")
      .select("id, name, price, is_active, stock_quantity")
      .in("id", [topPair.a, topPair.b]);
    const prods = (prodRows ?? []) as ProductRow[];
    const validPair = prods.length === 2 &&
      prods.every((p) => p.is_active && p.stock_quantity > 0);

    if (!validPair) {
      return new Response(
        JSON.stringify({
          pairs: ranked.length,
          top_pair: topPair,
          reason: "top_pair_invalid",
          products: prods,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const cooldownSince = new Date(now - COOLDOWN_DAYS * dayMs).toISOString();
    const { data: recentPromos } = await supabase
      .from("promotions")
      .select("id, product_ids, name, is_active, created_at")
      .gte("created_at", cooldownSince)
      .like("name", "BUNDLE%")
      .limit(50);

    const sortedTopIds = [topPair.a, topPair.b].sort();
    const alreadyPromoted = (recentPromos ?? []).some((p) => {
      const ids = (p.product_ids ?? []) as string[];
      if (ids.length !== 2) return false;
      const sorted = [...ids].sort();
      return sorted[0] === sortedTopIds[0] && sorted[1] === sortedTopIds[1];
    });

    if (alreadyPromoted) {
      return new Response(
        JSON.stringify({
          pairs: ranked.length,
          top_pair: topPair,
          reason: "cooldown_active",
          cooldown_days: COOLDOWN_DAYS,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const expiresAt = new Date(now + PROMO_TTL_DAYS * dayMs).toISOString();
    const startsAt = new Date(now).toISOString();
    const codeSuffix = Math.random().toString(36).slice(2, 6).toUpperCase();
    const promoCode = `BUNDLE${DISCOUNT_PCT}${codeSuffix}`;
    const promoName = `BUNDLE: ${topPair.a_name} + ${topPair.b_name}`;

    const proposedChange = {
      kind: "bundle_promo",
      promotion: {
        name: promoName,
        description: `Авто-промо: топ co-purchase пара (${topPair.count}×/${LOOKBACK_DAYS}д). −${DISCOUNT_PCT}% при покупці обох товарів.`,
        discount_type: "percentage",
        discount_value: DISCOUNT_PCT,
        product_ids: sortedTopIds,
        starts_at: startsAt,
        ends_at: expiresAt,
        is_active: true,
      },
      promo_code: {
        code: promoCode,
        discount_type: "percentage",
        discount_value: DISCOUNT_PCT,
        min_order_amount: MIN_ORDER,
        max_uses: 200,
        starts_at: startsAt,
        ends_at: expiresAt,
        is_active: true,
      },
      pair: topPair,
    };

    if (dryRun) {
      return new Response(
        JSON.stringify({
          pairs: ranked.length,
          top_pair: topPair,
          would_propose: proposedChange,
          top_5: ranked.slice(0, 5),
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // ── Branch A: hand off to Tribunal instead of writing directly ──
    const enq = await enqueueTribunalCase({
      source_function: "acos-bundle-auto-promote",
      category: "promo",
      urgency: "normal",
      proposed_change: proposedChange,
      context: {
        lookback_days: LOOKBACK_DAYS,
        co_occurrences: topPair.count,
        combined_revenue: topPair.combined_revenue,
        product_ids: sortedTopIds,
      },
      expected_impact: `+${Math.round(topPair.count * 0.1 * MIN_ORDER * 0.9).toLocaleString()}₴/30д at 10% redemption`,
    });

    __agent.success();
    return new Response(
      JSON.stringify({
        queued: true,
        case_id: enq.case_id,
        reused: enq.reused,
        previous_verdict: enq.previous_verdict ?? null,
        pair: topPair,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    __agent.error(err);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});

// ────────────────────────────────────────────────────────────────────────────
// Enforcer call-back: actually create the promotion + promo_code that the
// Tribunal approved. `proposed_change` matches what we enqueued above.
// `conditions` may carry a TTL override or rollout hint from the judge.
async function applyApprovedBundle(
  supabase: any,
  body: { proposed_change?: Record<string, unknown>; conditions?: Record<string, unknown>; case_id?: string },
): Promise<Response> {
  const change = body.proposed_change as
    | { promotion?: Record<string, unknown>; promo_code?: Record<string, unknown>; pair?: PairStat }
    | undefined;
  if (!change?.promotion || !change.promo_code) {
    return new Response(
      JSON.stringify({ ok: false, error: "missing_proposed_change" }),
      { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // Optional judge override: shorter TTL.
  const ttlHours = (body.conditions as { ttl_hours?: number } | undefined)?.ttl_hours;
  if (typeof ttlHours === "number" && ttlHours > 0) {
    const newEnd = new Date(Date.now() + ttlHours * 60 * 60 * 1000).toISOString();
    (change.promotion as Record<string, unknown>).ends_at = newEnd;
    (change.promo_code as Record<string, unknown>).ends_at = newEnd;
  }

  const { data: promotion, error: promoErr } = await supabase
    .from("promotions")
    .insert(change.promotion)
    .select("id, name")
    .single();

  if (promoErr || !promotion) {
    return new Response(
      JSON.stringify({ ok: false, error: "promotion_insert_failed", details: promoErr?.message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  const { data: code, error: codeErr } = await supabase
    .from("promo_codes")
    .insert(change.promo_code)
    .select("id, code")
    .single();

  if (codeErr || !code) {
    await supabase.from("promotions").update({ is_active: false }).eq("id", promotion.id);
    return new Response(
      JSON.stringify({ ok: false, error: "promo_code_insert_failed", details: codeErr?.message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  await supabase.from("ai_insights").insert({
    insight_type: "bundle_auto_promoted",
    title: `Tribunal схвалив BUNDLE: ${change.pair?.a_name ?? "?"} + ${change.pair?.b_name ?? "?"}`,
    description: `Створено promotion "${promotion.name}" і промокод ${code.code} після перевірки Tribunal (case ${body.case_id ?? "?"}).`,
    confidence: 0.8,
    risk_level: "low",
    affected_layer: "merchandising",
    status: "new",
    metrics: {
      promo_code: code.code,
      promotion_id: promotion.id,
      tribunal_case_id: body.case_id,
    },
  });
    return new Response(
    JSON.stringify({ ok: true, promotion_id: promotion.id, promo_code: code.code }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
}
