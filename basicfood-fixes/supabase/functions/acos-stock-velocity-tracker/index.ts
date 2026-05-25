// ACOS Stock Velocity Tracker
// Computes per-product sales velocity (7d & 30d), trend, days of supply
// and a restock recommendation. Writes one row per product into
// stock_velocity_snapshots (upsert).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const SAFETY_DAYS = 7; // we want ≥7 days of cover after restock
const REORDER_BUFFER_DAYS = 5; // trigger restock when only 5 days left

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const now = Date.now();
    const since30 = new Date(now - 30 * 86400 * 1000).toISOString();
    const since7 = new Date(now - 7 * 86400 * 1000).toISOString();

    const { data: products } = await supabase
      .from("products")
      .select("id, name, stock_quantity")
      .eq("is_active", true);

    const { data: items } = await supabase
      .from("order_items")
      .select("product_id, quantity, created_at")
      .gte("created_at", since30);

    const sold30 = new Map<string, number>();
    const sold7 = new Map<string, number>();
    const cutoff7 = new Date(since7).getTime();
    for (const it of items ?? []) {
      if (!it.product_id) continue;
      sold30.set(it.product_id, (sold30.get(it.product_id) ?? 0) + (it.quantity ?? 0));
      if (new Date(it.created_at).getTime() >= cutoff7) {
        sold7.set(it.product_id, (sold7.get(it.product_id) ?? 0) + (it.quantity ?? 0));
      }
    }

    let updated = 0;
    let restockNow = 0;
    let restockSoon = 0;
    let dormant = 0;
    let overstocked = 0;
    const snapshotRows: any[] = [];

    for (const p of products ?? []) {
      const s7 = sold7.get(p.id) ?? 0;
      const s30 = sold30.get(p.id) ?? 0;
      const v7 = s7 / 7;
      const v30 = s30 / 30;
      const stock = p.stock_quantity ?? 0;

      // Trend classification
      let trend: "accelerating" | "stable" | "decelerating" | "dormant" = "stable";
      if (s30 === 0) {
        trend = "dormant";
      } else if (v7 > v30 * 1.3) {
        trend = "accelerating";
      } else if (v7 < v30 * 0.6) {
        trend = "decelerating";
      }

      // Use a blended velocity (recent weighted higher) to project supply
      const vBlended = trend === "dormant" ? 0 : v7 * 0.6 + v30 * 0.4;
      const dos = vBlended > 0 ? stock / vBlended : (stock > 0 ? 999 : 0);
      const reorderPoint = Math.ceil(vBlended * REORDER_BUFFER_DAYS);
      const recommendedRestock = vBlended > 0
        ? Math.max(0, Math.ceil(vBlended * (SAFETY_DAYS + REORDER_BUFFER_DAYS) - stock))
        : 0;

      let recommendation: "restock_now" | "restock_soon" | "hold" | "overstocked" | "dormant" = "hold";
      if (trend === "dormant") {
        recommendation = stock > 0 ? "dormant" : "hold";
        if (recommendation === "dormant") dormant++;
      } else if (dos <= 3) {
        recommendation = "restock_now";
        restockNow++;
      } else if (dos <= REORDER_BUFFER_DAYS) {
        recommendation = "restock_soon";
        restockSoon++;
      } else if (vBlended > 0 && dos > 60) {
        recommendation = "overstocked";
        overstocked++;
      }

      // Confidence: more sales data → more confidence
      const confidence = Math.min(0.95, 0.5 + Math.min(s30, 30) / 60);

      snapshotRows.push({
        product_id: p.id,
        product_name: p.name,
        current_stock: stock,
        sold_7d: s7,
        sold_30d: s30,
        velocity_7d: Number(v7.toFixed(3)),
        velocity_30d: Number(v30.toFixed(3)),
        trend,
        days_of_supply: Number(dos.toFixed(1)),
        reorder_point: reorderPoint,
        recommended_restock: recommendedRestock,
        recommendation,
        confidence: Number(confidence.toFixed(2)),
        computed_at: new Date().toISOString(),
      });
    }

    // Batch upsert all snapshots.
    if (snapshotRows.length > 0) {
      const { error: upsertErr } = await supabase
        .from("stock_velocity_snapshots")
        .upsert(snapshotRows, { onConflict: "product_id" });
      if (!upsertErr) updated = snapshotRows.length;
    }

    // Roll up into ai_insights when there is something actionable
    if (restockNow > 0 || overstocked >= 3) {
      const { data: top } = await supabase
        .from("stock_velocity_snapshots")
        .select("product_name, days_of_supply, current_stock, recommended_restock, recommendation")
        .in("recommendation", ["restock_now", "restock_soon"])
        .order("days_of_supply", { ascending: true })
        .limit(8);

      const lines = (top ?? [])
        .map((t) => `• ${t.product_name}: ${Number(t.days_of_supply).toFixed(1)}д запасу → поповнити ${t.recommended_restock} од.`)
        .join("\n");

      await supabase.from("ai_insights").insert({
        insight_type: "stock_velocity",
        title: `📊 Stock velocity: ${restockNow} критичних, ${restockSoon} попереджень`,
        description:
          `Аналіз швидкості продажів за 7/30 днів виявив товари, які потребують уваги:\n\n${lines}` +
          (overstocked > 0 ? `\n\nЗаперезапасені позиції: ${overstocked}.` : "") +
          (dormant > 0 ? `\nНеактивні позиції з залишком: ${dormant}.` : ""),
        expected_impact: restockNow > 0
          ? "Запобігти out-of-stock та втраті продажів у наступні 3-5 днів."
          : "Оптимізувати рівень запасів і зменшити заморожений капітал.",
        confidence: 0.82,
        affected_layer: "inventory",
        risk_level: restockNow > 0 ? "high" : "medium",
        status: "new",
        metrics: { updated, restock_now: restockNow, restock_soon: restockSoon, overstocked, dormant },
      });
    }

    return new Response(
      JSON.stringify({ updated, restock_now: restockNow, restock_soon: restockSoon, overstocked, dormant }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
