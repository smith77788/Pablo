// ACOS Stock Anomaly Detector
// Scans the stock_adjustments stream for suspicious patterns and writes
// findings into stock_anomalies + ai_insights.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

type Adj = {
  id: string;
  product_id: string;
  product_name: string;
  delta: number;
  previous_quantity: number;
  new_quantity: number;
  adjustment_type: string;
  reason: string | null;
  source: string;
  performed_by_name: string | null;
  created_at: string;
};

const LARGE_DROP_THRESHOLD = 50; // units in single manual subtract
const FREQUENT_WINDOW_HOURS = 24;
const FREQUENT_THRESHOLD = 4; // >=4 corrections in 24h

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

    const sinceIso = new Date(Date.now() - FREQUENT_WINDOW_HOURS * 3600 * 1000).toISOString();

    const { data: adjustments } = await supabase
      .from("stock_adjustments")
      .select("id, product_id, product_name, delta, previous_quantity, new_quantity, adjustment_type, reason, source, performed_by_name, created_at")
      .gte("created_at", sinceIso)
      .order("created_at", { ascending: false })
      .limit(2000);

    const adjs: Adj[] = (adjustments ?? []) as Adj[];
    const byProduct = new Map<string, Adj[]>();
    for (const a of adjs) {
      if (!byProduct.has(a.product_id)) byProduct.set(a.product_id, []);
      byProduct.get(a.product_id)!.push(a);
    }

    let detected = 0;
    let critical = 0;
    const findings: Array<Record<string, unknown>> = [];

    for (const [pid, rows] of byProduct) {
      const productName = rows[0]?.product_name ?? "—";

      // 1. Large manual drop
      const bigDrop = rows.find((r) =>
        ["subtract", "correction", "damage"].includes(r.adjustment_type) &&
        Math.abs(r.delta) >= LARGE_DROP_THRESHOLD
      );
      if (bigDrop) {
        findings.push({
          product_id: pid,
          product_name: productName,
          anomaly_type: "large_manual_drop",
          severity: Math.abs(bigDrop.delta) >= 200 ? "critical" : "high",
          window_hours: FREQUENT_WINDOW_HOURS,
          description: `Велике ручне списання ${Math.abs(bigDrop.delta)} од. (${bigDrop.adjustment_type}) користувачем ${bigDrop.performed_by_name ?? "—"}.`,
          evidence: {
            adjustment_id: bigDrop.id,
            delta: bigDrop.delta,
            previous: bigDrop.previous_quantity,
            new: bigDrop.new_quantity,
            reason: bigDrop.reason,
            performed_by: bigDrop.performed_by_name,
            at: bigDrop.created_at,
          },
        });
      }

      // 2. Frequent corrections
      const corrections = rows.filter((r) => r.adjustment_type === "correction");
      if (corrections.length >= FREQUENT_THRESHOLD) {
        findings.push({
          product_id: pid,
          product_name: productName,
          anomaly_type: "frequent_corrections",
          severity: corrections.length >= 8 ? "high" : "medium",
          window_hours: FREQUENT_WINDOW_HOURS,
          description: `${corrections.length} корекцій залишку за ${FREQUENT_WINDOW_HOURS}год — можлива розсинхронізація обліку.`,
          evidence: {
            count: corrections.length,
            sample_ids: corrections.slice(0, 5).map((c) => c.id),
            net_delta: corrections.reduce((s, c) => s + c.delta, 0),
          },
        });
      }

      // 3. Unexplained zeroing (set to 0 without restock entry recorded since)
      const zeroed = rows.find((r) => r.adjustment_type === "set" && r.new_quantity === 0);
      if (zeroed) {
        const restocked = rows.some((r) => r.adjustment_type === "restock" && new Date(r.created_at) > new Date(zeroed.created_at));
        if (!restocked && (!zeroed.reason || zeroed.reason.length < 5)) {
          findings.push({
            product_id: pid,
            product_name: productName,
            anomaly_type: "unexplained_zeroing",
            severity: "medium",
            window_hours: FREQUENT_WINDOW_HOURS,
            description: `Залишок встановлено в 0 без супровідної причини або плану поповнення.`,
            evidence: {
              adjustment_id: zeroed.id,
              previous: zeroed.previous_quantity,
              reason: zeroed.reason,
              performed_by: zeroed.performed_by_name,
              at: zeroed.created_at,
            },
          });
        }
      }
    }

    // 4. Velocity mismatch — compare manual subtracts to recent sales (sample)
    if (byProduct.size > 0) {
      const productIds = Array.from(byProduct.keys());
      const { data: salesRows } = await supabase
        .from("order_items")
        .select("product_id, quantity, created_at")
        .in("product_id", productIds)
        .gte("created_at", sinceIso);
      const salesMap = new Map<string, number>();
      for (const s of salesRows ?? []) {
        if (!s.product_id) continue;
        salesMap.set(s.product_id, (salesMap.get(s.product_id) ?? 0) + (s.quantity ?? 0));
      }
      for (const [pid, rows] of byProduct) {
        const sold = salesMap.get(pid) ?? 0;
        const totalSubtract = rows
          .filter((r) => ["subtract", "correction", "damage"].includes(r.adjustment_type))
          .reduce((s, r) => s + Math.abs(r.delta), 0);
        // If we manually removed >2x what we actually sold, flag it.
        if (totalSubtract >= 20 && totalSubtract > sold * 2 + 10) {
          findings.push({
            product_id: pid,
            product_name: rows[0]?.product_name ?? "—",
            anomaly_type: "velocity_mismatch",
            severity: "medium",
            window_hours: FREQUENT_WINDOW_HOURS,
            description: `Ручні списання (${totalSubtract} од.) суттєво перевищують фактичні продажі (${sold} од.).`,
            evidence: { manual_subtract: totalSubtract, sold, ratio: sold === 0 ? null : Number((totalSubtract / sold).toFixed(2)) },
          });
        }
      }
    }

    // Batch dedup check: pull open anomalies for all finding product+type pairs in one query.
    const findingKeys = findings.map((f) => `${f.product_id}|${f.anomaly_type}`);
    const findingProductIds = [...new Set(findings.map((f) => f.product_id as string))];
    let existingKeys = new Set<string>();
    if (findingProductIds.length > 0) {
      const { data: openAnomalies } = await supabase
        .from("stock_anomalies")
        .select("product_id, anomaly_type")
        .in("product_id", findingProductIds)
        .eq("status", "open")
        .gte("detected_at", sinceIso);
      existingKeys = new Set((openAnomalies ?? []).map((a) => `${a.product_id}|${a.anomaly_type}`));
    }
    const newFindings = findings.filter((f) => !existingKeys.has(`${f.product_id}|${f.anomaly_type}`));

    // Batch insert all new (non-duplicate) findings.
    if (newFindings.length > 0) {
      const { data: inserted, error: batchErr } = await supabase.from("stock_anomalies").insert(newFindings).select("severity");
      if (!batchErr) {
        for (const row of inserted ?? []) {
          detected++;
          if (row.severity === "critical") critical++;
        }
      }
    }

    // Roll up into ai_insights only if we have something material
    if (detected > 0) {
      await supabase.from("ai_insights").insert({
        insight_type: "stock_anomaly",
        title: `📦 Виявлено ${detected} аномалій у журналі залишків`,
        description: findings.slice(0, 8).map((f) => `• ${f.product_name}: ${f.description}`).join("\n"),
        expected_impact: critical > 0
          ? "Можливі втрати від нестачі/розсинхронізації запасів — рекомендуємо ручну перевірку."
          : "Поліпшення точності обліку запасів та довіри до журналу.",
        confidence: 0.8,
        affected_layer: "inventory",
        risk_level: critical > 0 ? "high" : "medium",
        status: "new",
        metrics: { detected, critical, window_hours: FREQUENT_WINDOW_HOURS },
      });
    }

    return new Response(
      JSON.stringify({ scanned: adjs.length, products: byProduct.size, detected, critical }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
