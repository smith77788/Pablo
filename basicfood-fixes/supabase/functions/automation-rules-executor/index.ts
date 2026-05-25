// ACOS Section 10 — Automation Rules Executor
// Reads enabled rules from automation_rules, evaluates triggers against
// products + product_stats, performs actions (disable_product, create_insight,
// send_alert), updates run_count + last_run_at. Idempotent via insight dedup_bucket.
//
// Designed to run hourly via pg_cron. Safe to invoke manually from admin.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

interface Rule {
  id: string;
  name: string;
  trigger_type: string;
  condition: Record<string, unknown>;
  action_type: string;
  parameters: Record<string, unknown>;
  is_enabled: boolean;
  run_count: number;
}

interface ExecutionResult {
  rule_id: string;
  rule_name: string;
  trigger_type: string;
  action_type: string;
  matched: number;
  applied: number;
  errors: string[];
}

const num = (v: unknown, d: number) => (typeof v === "number" ? v : d);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    // Refresh product_stats so rules see fresh CTR / conv_rate
    try {
      await supabase.rpc("refresh_product_stats");
    } catch (_e) {
      // non-fatal — continue with existing stats
    }

    const { data: rules, error: rulesErr } = await supabase
      .from("automation_rules")
      .select("*")
      .eq("is_enabled", true);

    if (rulesErr) throw rulesErr;

    const results: ExecutionResult[] = [];

    for (const rule of (rules ?? []) as Rule[]) {
      const result: ExecutionResult = {
        rule_id: rule.id,
        rule_name: rule.name,
        trigger_type: rule.trigger_type,
        action_type: rule.action_type,
        matched: 0,
        applied: 0,
        errors: [],
      };

      try {
        switch (rule.trigger_type) {
          case "stock_zero": {
            const { data: zeroStock } = await supabase
              .from("products")
              .select("id, name")
              .eq("is_active", true)
              .eq("stock_quantity", 0);

            const targets = zeroStock ?? [];
            result.matched = targets.length;

            if (rule.action_type === "disable_product" && targets.length > 0) {
              const { error } = await supabase
                .from("products")
                .update({ is_active: false })
                .in("id", targets.map((p) => p.id));
              if (error) result.errors.push(error.message);
              else {
                result.applied = targets.length;
                // Log a single summary insight so admin sees what happened
                await supabase.from("ai_insights").insert({
                  insight_type: "auto_disabled_oos",
                  title: `Auto-disabled ${targets.length} out-of-stock product(s)`,
                  description: targets.map((p) => `• ${p.name}`).join("\n"),
                  expected_impact: "Prevents users seeing unavailable items in catalog/Shopping feed",
                  affected_layer: "products",
                  risk_level: "low",
                  status: "new",
                  confidence: 1.0,
                  metrics: { product_ids: targets.map((p) => p.id), count: targets.length },
                });
              }
            }
            break;
          }

          case "low_ctr": {
            const ctrThreshold = num(rule.condition.ctr_threshold, 0.02);
            const minViews = num(rule.condition.min_views, 100);
            const { data: stats } = await supabase.rpc("get_product_stats");
            const lowCtr = (stats ?? []).filter(
              (s: any) => s.is_active && s.views_7d >= minViews && s.ctr < ctrThreshold,
            );
            result.matched = lowCtr.length;

            if (rule.action_type === "create_insight" && lowCtr.length > 0) {
              const insightRows = lowCtr.map((p) => ({
                insight_type: "low_ctr_product",
                title: `Low CTR: ${p.name} (${(p.ctr * 100).toFixed(2)}%)`,
                description: `Product has ${p.views_7d} impressions in 7d but CTR ${(p.ctr * 100).toFixed(2)}% < threshold ${(ctrThreshold * 100).toFixed(1)}%. Consider improving thumbnail, title, or first-line description.`,
                expected_impact: "Improving CTR by 1pp on this SKU could add ~" + Math.round(p.views_7d * 0.01) + " clicks/week",
                affected_layer: "products",
                risk_level: (rule.parameters.risk_level as string) ?? "medium",
                status: "new",
                confidence: 0.85,
                metrics: {
                  product_id: p.product_id,
                  views_7d: p.views_7d,
                  clicks_7d: p.clicks_7d,
                  ctr: p.ctr,
                  threshold: ctrThreshold,
                },
              }));
              const { error } = await supabase.from("ai_insights").insert(insightRows);
              if (error && !error.message.includes("duplicate")) result.errors.push(error.message);
              else if (!error) result.applied = insightRows.length;
            }
            break;
          }

          case "seo_gap": {
            const fields = (rule.condition.check_fields as string[]) ?? ["description"];
            const { data: products } = await supabase
              .from("products")
              .select("id, name, description, composition")
              .eq("is_active", true);

            const gaps = (products ?? []).filter((p: any) => {
              return fields.some((f) => {
                const v = p[f];
                return !v || (typeof v === "string" && v.trim().length < 20);
              });
            });
            result.matched = gaps.length;

            if (rule.action_type === "create_insight" && gaps.length > 0) {
              const { error } = await supabase.from("ai_insights").insert({
                insight_type: "missing_seo_content",
                title: `SEO gap: ${gaps.length} product(s) missing content`,
                description: gaps.slice(0, 10).map((p: any) => `• ${p.name}`).join("\n") +
                  (gaps.length > 10 ? `\n…+${gaps.length - 10} more` : ""),
                expected_impact: "Filling descriptions improves Google indexing and Shopping feed approval rate",
                affected_layer: "seo",
                risk_level: (rule.parameters.risk_level as string) ?? "low",
                status: "new",
                confidence: 0.9,
                metrics: { product_ids: gaps.map((p: any) => p.id), missing_fields: fields },
              });
              if (error && !error.message.includes("duplicate")) result.errors.push(error.message);
              else if (!error) result.applied = 1;
            }
            break;
          }

          case "conversion_drop": {
            // Compare last 7d vs previous 7d using events table
            const now = new Date();
            const d7 = new Date(now.getTime() - 7 * 86400000).toISOString();
            const d14 = new Date(now.getTime() - 14 * 86400000).toISOString();

            const [
              { count: views7 },
              { count: purch7 },
              { count: viewsPrev },
              { count: purchPrev },
            ] = await Promise.all([
              supabase.from("events").select("id", { count: "exact", head: true }).eq("event_type", "product_viewed").gte("created_at", d7),
              supabase.from("events").select("id", { count: "exact", head: true }).eq("event_type", "purchase_completed").gte("created_at", d7),
              supabase.from("events").select("id", { count: "exact", head: true }).eq("event_type", "product_viewed").gte("created_at", d14).lt("created_at", d7),
              supabase.from("events").select("id", { count: "exact", head: true }).eq("event_type", "purchase_completed").gte("created_at", d14).lt("created_at", d7),
            ]);

            const cur = (views7 ?? 0) > 0 ? (purch7 ?? 0) / (views7 ?? 1) : 0;
            const prev = (viewsPrev ?? 0) > 0 ? (purchPrev ?? 0) / (viewsPrev ?? 1) : 0;
            const drop = prev > 0 ? (prev - cur) / prev : 0;
            const threshold = num(rule.condition.drop_threshold, 0.3);

            if (drop >= threshold && prev > 0) {
              result.matched = 1;
              if (rule.action_type === "send_alert" || rule.action_type === "create_insight") {
                const { error } = await supabase.from("ai_insights").insert({
                  insight_type: "conversion_drop",
                  title: `Conversion dropped ${(drop * 100).toFixed(1)}% week-over-week`,
                  description: `Last 7d conv: ${(cur * 100).toFixed(2)}% (${purch7}/${views7})\nPrev 7d conv: ${(prev * 100).toFixed(2)}% (${purchPrev}/${viewsPrev})`,
                  expected_impact: "Investigate checkout funnel, payment method failures, recent UI changes",
                  affected_layer: "funnel",
                  risk_level: "high",
                  status: "new",
                  confidence: 0.9,
                  metrics: { current_conv: cur, previous_conv: prev, drop_pct: drop, views_7d: views7, purchases_7d: purch7 },
                });
                if (error && !error.message.includes("duplicate")) result.errors.push(error.message);
                else if (!error) result.applied = 1;
              }
            }
            break;
          }

          default:
            result.errors.push(`Unknown trigger_type: ${rule.trigger_type}`);
        }

        // Update rule run stats
        await supabase
          .from("automation_rules")
          .update({ last_run_at: new Date().toISOString(), run_count: rule.run_count + 1 })
          .eq("id", rule.id);
      } catch (e) {
        result.errors.push(String((e as Error).message ?? e));
      }

      results.push(result);
    }

    return new Response(
      JSON.stringify({
        ok: true,
        ran_at: new Date().toISOString(),
        rules_evaluated: results.length,
        results,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ ok: false, error: String((e as Error).message ?? e) }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
