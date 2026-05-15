// ACOS Auto-Promotion Engine — runs daily via pg_cron.
// Scans products with high views but low conversion rate over the last 7 days.
// If CR < 2% with ≥30 views → auto-creates a -10% promotion for 72 hours.
// Skips products with an active [AUTO] promotion (cooldown via existing record).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const MIN_VIEWS = 30;
const MAX_CR = 0.02; // 2%
const DISCOUNT_PCT = 10;
const DURATION_HOURS = 72;
const LOOKBACK_DAYS = 7;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const since = new Date(Date.now() - LOOKBACK_DAYS * 24 * 60 * 60 * 1000).toISOString();

    // 1. Pull all view + purchase events in window
    const { data: viewEvents } = await supabase
      .from("events")
      .select("product_id")
      .eq("event_type", "product_viewed")
      .not("product_id", "is", null)
      .gte("created_at", since);

    const { data: purchaseEvents } = await supabase
      .from("events")
      .select("product_id")
      .eq("event_type", "purchase_completed")
      .not("product_id", "is", null)
      .gte("created_at", since);

    if (!viewEvents || viewEvents.length === 0) {
      return new Response(
        JSON.stringify({ scanned: 0, promoted: 0, reason: "no_views" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Aggregate views & purchases per product
    const viewCount = new Map<string, number>();
    for (const e of viewEvents) {
      if (e.product_id) viewCount.set(e.product_id, (viewCount.get(e.product_id) ?? 0) + 1);
    }
    const purchaseCount = new Map<string, number>();
    for (const e of purchaseEvents ?? []) {
      if (e.product_id) purchaseCount.set(e.product_id, (purchaseCount.get(e.product_id) ?? 0) + 1);
    }

    // 3. Identify stale products
    const stale: Array<{ product_id: string; views: number; purchases: number; cr: number }> = [];
    for (const [pid, views] of viewCount.entries()) {
      if (views < MIN_VIEWS) continue;
      const purchases = purchaseCount.get(pid) ?? 0;
      const cr = purchases / views;
      if (cr < MAX_CR) stale.push({ product_id: pid, views, purchases, cr });
    }

    if (stale.length === 0) {
      return new Response(
        JSON.stringify({ scanned: viewCount.size, promoted: 0, reason: "all_healthy" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 4. Skip products with an active [AUTO] promotion already
    const productIds = stale.map((s) => s.product_id);
    const { data: activePromos } = await supabase
      .from("promotions")
      .select("product_ids, name, ends_at")
      .eq("is_active", true)
      .like("name", "[AUTO]%");

    const blocked = new Set<string>();
    const now = Date.now();
    for (const p of activePromos ?? []) {
      if (p.ends_at && new Date(p.ends_at).getTime() < now) continue;
      for (const pid of (p.product_ids ?? []) as string[]) {
        if (productIds.includes(pid)) blocked.add(pid);
      }
    }

    const toPromote = stale.filter((s) => !blocked.has(s.product_id));
    if (toPromote.length === 0) {
      return new Response(
        JSON.stringify({ scanned: viewCount.size, stale: stale.length, promoted: 0, reason: "all_in_cooldown" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 5. Fetch product names for nicer audit trail
    const { data: prodRows } = await supabase
      .from("products")
      .select("id, name")
      .in("id", toPromote.map((p) => p.product_id));
    const nameMap = new Map<string, string>();
    for (const p of prodRows ?? []) nameMap.set(p.id, p.name);

    // 6. Create promotions + insights (parallel per item)
    const endsAt = new Date(Date.now() + DURATION_HOURS * 60 * 60 * 1000).toISOString();
    const startsAt = new Date().toISOString();

    const promoteResults = await Promise.all(toPromote.map(async (item) => {
      const pname = nameMap.get(item.product_id) ?? item.product_id.slice(0, 8);
      const { error: promoErr } = await supabase.from("promotions").insert({
        name: `[AUTO] -${DISCOUNT_PCT}% ${pname}`,
        description: `Auto-generated. CR ${(item.cr * 100).toFixed(2)}% (${item.purchases}/${item.views}) over ${LOOKBACK_DAYS}d.`,
        discount_type: "percentage",
        discount_value: DISCOUNT_PCT,
        product_ids: [item.product_id],
        starts_at: startsAt,
        ends_at: endsAt,
        is_active: true,
      });
      if (promoErr) return false;
      await supabase.from("ai_insights").insert({
        insight_type: "auto_promotion",
        title: `Auto-promo -${DISCOUNT_PCT}% on "${pname}"`,
        description: `CR ${(item.cr * 100).toFixed(2)}% (${item.purchases} purchases / ${item.views} views) below threshold ${(MAX_CR * 100).toFixed(0)}%. Promotion active for ${DURATION_HOURS}h.`,
        expected_impact: `+15-25% conversion rate during promo window`,
        confidence: 0.86,
        risk_level: "low",
        affected_layer: "website",
        status: "applied",
        metrics: {
          product_id: item.product_id,
          views: item.views,
          purchases: item.purchases,
          cr_before: item.cr,
          discount_pct: DISCOUNT_PCT,
          duration_hours: DURATION_HOURS,
        },
      });
      return true;
    }));
    const promoted = promoteResults.filter(Boolean).length;

    return new Response(
      JSON.stringify({
        scanned: viewCount.size,
        stale: stale.length,
        in_cooldown: stale.length - toPromote.length,
        promoted,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
