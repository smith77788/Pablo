// Cycle #20 — Funnel Drop-Off Healer (v2)
// Why v2: original used `product_view`/`purchase` but real events are
// `product_viewed`/`purchase_completed` — so the funnel always reported
// 100% drop and produced false alarms. v2 uses the actual event taxonomy
// AND adds the cart_viewed / checkout_viewed mid-funnel steps so we can
// pinpoint where users drop *before* clicking "Оформити".
//
// Real funnel: product_viewed → add_to_cart → cart_viewed → checkout_viewed → begin_checkout → purchase_completed
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const STEPS = [
  "product_viewed",
  "add_to_cart",
  "cart_viewed",
  "checkout_viewed",
  "begin_checkout",
  "purchase_completed",
] as const;

const FIX_HINTS: Record<string, string> = {
  product_viewed_to_add_to_cart:
    "Підсиль картку: галерея, відгуки, ціна-якоря, кнопка 'Купити' над фолдом.",
  add_to_cart_to_cart_viewed:
    "Користувач додав у кошик, але не відкрив його. Покажи toast із CTA 'Переглянути кошик' або mini-cart preview.",
  cart_viewed_to_checkout_viewed:
    "Втрата у кошику. Зроби кнопку 'Оформити' помітнішою, додай free-shipping progress, прибери відволікання.",
  checkout_viewed_to_begin_checkout:
    "Юзер відкрив checkout, але не натиснув 'Оформити'. Спрости форму, автозаповнення Нової Пошти, гостьовий режим.",
  begin_checkout_to_purchase_completed:
    "Юзер натиснув 'Оформити' але оплата/підтвердження не дійшло. Перевір помилки monobank/мережі, додай fallback методи.",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-funnel-dropoff-healer", req, null, async () => {
    const __res = await (async () => {

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const sinceISO = new Date(Date.now() - 14 * 86_400_000).toISOString();

    // Count distinct sessions per step in parallel — a session that fired
    // add_to_cart 3 times shouldn't inflate the funnel. This gives a true
    // "how many users reached this step" measurement.
    const stepResults = await Promise.all(
      STEPS.map((step) =>
        supabase.from("events").select("session_id").eq("event_type", step)
          .gte("created_at", sinceISO).not("session_id", "is", null).limit(50000)
      ),
    );
    const counts: Record<string, number> = Object.fromEntries(
      STEPS.map((step, i) => {
        const { data, error } = stepResults[i];
        if (error) {
          console.error(`Failed to count ${step}:`, error);
          return [step, 0];
        }
        return [step, new Set((data ?? []).map((r: any) => r.session_id)).size];
      }),
    );

    const rows: any[] = [];
    let worstDrop = 0;
    let worstStep = "";

    for (let i = 1; i < STEPS.length; i++) {
      const prev = counts[STEPS[i - 1]];
      const curr = counts[STEPS[i]];
      if (prev === 0) continue;
      const dropRatio = Math.max(0, 1 - curr / prev);
      const fixKey = `${STEPS[i - 1]}_to_${STEPS[i]}`;
      let severity = "low";
      if (dropRatio > 0.9) severity = "critical";
      else if (dropRatio > 0.75) severity = "high";
      else if (dropRatio > 0.5) severity = "medium";

      rows.push({
        page_path: "/funnel/site",
        step_name: fixKey,
        step_index: i,
        prev_count: prev,
        step_count: curr,
        drop_ratio: dropRatio,
        severity,
        recommended_fix: FIX_HINTS[fixKey] ?? "Дослідити крок вручну.",
        status: "new",
        computed_at: new Date().toISOString(),
      });

      if (dropRatio > worstDrop) {
        worstDrop = dropRatio;
        worstStep = fixKey;
      }
    }

    if (rows.length) {
      await supabase.from("funnel_dropoff_signals").delete().eq("status", "new").eq("page_path", "/funnel/site");
      await supabase.from("funnel_dropoff_signals").insert(rows);
    }

    if (worstStep && worstDrop > 0.5) {
      await supabase.from("ai_insights").insert({
        insight_type: "funnel_dropoff",
        title: `Найбільший провал у воронці: ${worstStep} (${Math.round(worstDrop * 100)}%)`,
        description: `${FIX_HINTS[worstStep] ?? "Перевірити крок."} Дані за 14 днів (унікальні сесії).`,
        confidence: 0.8,
        risk_level: worstDrop > 0.9 ? "high" : "medium",
        affected_layer: "website",
        metrics: { counts, worst_step: worstStep, worst_drop: worstDrop },
      });
    }

    return new Response(JSON.stringify({ ok: true, counts, worst_step: worstStep, worst_drop: worstDrop }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("funnel-dropoff error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
