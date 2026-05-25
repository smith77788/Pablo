import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Predictive LTV (simple BG/NBD-inspired heuristic):
// LTV = AOV * predicted_purchases_next_12mo
// predicted_purchases ~ frequency_per_year * survival_factor(recency)
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

    const since365 = new Date(Date.now() - 365 * 86400_000).toISOString();

    const { data: orders } = await supabase
      .from("orders")
      .select("id, customer_phone, customer_email, customer_name, total, created_at, status")
      .gte("created_at", since365)
      .in("status", ["new", "processing", "shipped", "delivered", "completed"])
      .limit(5000);

    type Bucket = { name: string; phone: string | null; email: string | null; orders: number; spent: number; first: number; last: number };
    const map = new Map<string, Bucket>();

    for (const o of orders ?? []) {
      const key = (o.customer_phone || o.customer_email || "").toString().trim().toLowerCase();
      if (!key) continue;
      const ts = new Date(o.created_at).getTime();
      const b = map.get(key) ?? {
        name: o.customer_name,
        phone: o.customer_phone,
        email: o.customer_email,
        orders: 0,
        spent: 0,
        first: ts,
        last: 0,
      };
      b.orders++;
      b.spent += o.total ?? 0;
      b.first = Math.min(b.first, ts);
      b.last = Math.max(b.last, ts);
      b.name = b.name || o.customer_name;
      map.set(key, b);
    }

    const now = Date.now();
    const scores: Array<{
      key: string;
      name: string;
      phone: string | null;
      email: string | null;
      orders: number;
      aov: number;
      recency_days: number;
      tenure_days: number;
      ltv_12m: number;
      tier: "vip" | "high" | "mid" | "low";
    }> = [];

    for (const [key, b] of map) {
      const aov = b.spent / b.orders;
      const recencyDays = (now - b.last) / 86400_000;
      const tenureDays = Math.max(1, (now - b.first) / 86400_000);
      const freqPerYear = (b.orders / tenureDays) * 365;
      // Survival factor: linear decay, full at 0d, 0 at 180d+
      const survival = Math.max(0, 1 - recencyDays / 180);
      const predicted = freqPerYear * survival;
      const ltv = Math.round(aov * predicted);

      let tier: "vip" | "high" | "mid" | "low";
      if (ltv >= 3000) tier = "vip";
      else if (ltv >= 1500) tier = "high";
      else if (ltv >= 500) tier = "mid";
      else tier = "low";

      scores.push({
        key,
        name: b.name,
        phone: b.phone,
        email: b.email,
        orders: b.orders,
        aov: Math.round(aov),
        recency_days: Math.round(recencyDays),
        tenure_days: Math.round(tenureDays),
        ltv_12m: ltv,
        tier,
      });
    }

    scores.sort((a, b) => b.ltv_12m - a.ltv_12m);

    const tiers = { vip: 0, high: 0, mid: 0, low: 0 };
    for (const s of scores) tiers[s.tier]++;
    const totalLtv = scores.reduce((sum, s) => sum + s.ltv_12m, 0);
    const top20 = scores.slice(0, 20);

    // Sync VIP tags into customers table (batch by phone then email)
    const vips = scores.filter((x) => x.tier === "vip");
    const vipPhones = vips.filter((v) => v.phone).map((v) => v.phone!);
    const vipEmails = vips.filter((v) => !v.phone && v.email).map((v) => v.email!);
    const [byPhone, byEmail] = await Promise.all([
      vipPhones.length > 0
        ? supabase.from("customers").select("id, tags, phone").in("phone", vipPhones)
        : Promise.resolve({ data: [] as any[] }),
      vipEmails.length > 0
        ? supabase.from("customers").select("id, tags, email").in("email", vipEmails)
        : Promise.resolve({ data: [] as any[] }),
    ]);
    const toTag = [...(byPhone.data ?? []), ...(byEmail.data ?? [])];
    let vipTagged = 0;
    if (toTag.length > 0) {
      const tagResults = await Promise.all(
        toTag.map(async (c: any) => {
          const tags = Array.from(new Set([...(c.tags ?? []), "💎 VIP"]));
          const { error } = await supabase.from("customers").update({ tags }).eq("id", c.id);
          return error ? 0 : 1;
        }),
      );
      vipTagged = tagResults.reduce((s, n) => s + n, 0);
    }

    if (scores.length > 0) {
      const lines = top20
        .slice(0, 10)
        .map((s) => `• ${s.name} — ${s.ltv_12m.toLocaleString("uk-UA")}₴ (${s.orders}×${s.aov}₴, ${s.tier})`)
        .join("\n");

      await supabase.from("ai_insights").insert({
        insight_type: "ltv_scoring",
        title: `💎 LTV-сегментація: ${tiers.vip} VIP / ${tiers.high} high / ${tiers.mid} mid`,
        description: `Прогнозований 12-міс LTV для ${scores.length} клієнтів. Сумарний прогноз: ${totalLtv.toLocaleString("uk-UA")}₴.\n\nТоп-10:\n${lines}`,
        expected_impact: tiers.vip > 0 ? `Фокус на ${tiers.vip} VIP-клієнтах = ~${Math.round(totalLtv * 0.4).toLocaleString("uk-UA")}₴ потенціалу` : "Накопичуємо дані",
        confidence: 0.8,
        affected_layer: "crm",
        risk_level: "low",
        status: "new",
        metrics: { total: scores.length, total_ltv: totalLtv, tiers, vip_tagged: vipTagged, top: top20 },
      });
    }

    return new Response(
      JSON.stringify({ total: scores.length, total_ltv: totalLtv, tiers, vip_tagged: vipTagged, top: top20 }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
