// ACOS Anti-Fraud Guard
//
// Scans recent orders for risk signals and writes to order_fraud_signals.
// Heuristics (cheap, no ML, no external calls):
//   • Disposable email domains (mailinator, tempmail, guerrillamail, etc.)
//   • Phone format anomalies (too short, all-same-digits)
//   • Abnormal order amount (> 3× rolling 30d AOV)
//   • Velocity: same phone/email > 3 orders in 24h
//   • Address looks placeholder ("test", "asdf", "123")
//   • Customer brand-new + amount > 2× AOV (cold-stranger big-bag risk)
//
// Risk score 0..100. Levels: low <30, medium 30-60, high >60.
// HIGH risk → also seed an ai_insights record + Telegram alert to admins.
//
// Idempotent: order_id is unique; uses upsert.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};
const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

const DISPOSABLE_DOMAINS = new Set([
  "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
  "throwaway.email", "yopmail.com", "trashmail.com", "fakeinbox.com",
  "mintemail.com", "sharklasers.com", "maildrop.cc", "getairmail.com",
  "tempr.email", "dispostable.com", "moakt.com", "spamgourmet.com",
]);

const PLACEHOLDER_PATTERNS = [
  /^test/i, /^asdf/i, /^123+$/, /^q+w+e+/i, /^xxx+/i, /lorem/i, /ipsum/i,
];

const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-anti-fraud-guard", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));
    const lookbackHours = Number(body?.lookback_hours) || 24;
    const sinceISO = new Date(Date.now() - lookbackHours * 3600_000).toISOString();

    // Rolling 30d AOV baseline
    const { data: aovRows } = await supabase
      .from("orders")
      .select("total")
      .neq("status", "cancelled")
      .gte("created_at", new Date(Date.now() - 30 * 86400_000).toISOString())
      .limit(2000);
    const totals = (aovRows ?? []).map((r) => r.total ?? 0).filter((n) => n > 0);
    const aov = totals.length ? totals.reduce((s, n) => s + n, 0) / totals.length : 1000;

    // Recent orders
    const { data: orders } = await supabase
      .from("orders")
      .select("id, customer_name, customer_email, customer_phone, delivery_address, total, created_at, user_id, source, message")
      .gte("created_at", sinceISO)
      .order("created_at", { ascending: false })
      .limit(500);

    // Skip already-scanned orders
    const orderIds = (orders ?? []).map((o) => o.id);
    const { data: existing } = orderIds.length
      ? await supabase.from("order_fraud_signals").select("order_id").in("order_id", orderIds)
      : { data: [] as { order_id: string }[] };
    const scanned = new Set((existing ?? []).map((e) => e.order_id));

    // Velocity precompute (group by phone/email in window)
    const velocityPhone = new Map<string, number>();
    const velocityEmail = new Map<string, number>();
    for (const o of orders ?? []) {
      if (o.customer_phone) {
        velocityPhone.set(o.customer_phone, (velocityPhone.get(o.customer_phone) ?? 0) + 1);
      }
      if (o.customer_email) {
        const e = o.customer_email.toLowerCase();
        velocityEmail.set(e, (velocityEmail.get(e) ?? 0) + 1);
      }
    }

    const newSignals: any[] = [];
    const insightRows: any[] = [];
    const adminAlerts: { order: any; reasons: string[]; score: number }[] = [];

    for (const o of orders ?? []) {
      if (scanned.has(o.id)) continue;
      if (o.message === "seed") continue; // skip synthetic

      const reasons: string[] = [];
      const flags: Record<string, unknown> = {};
      let score = 0;

      // Email signals
      if (o.customer_email) {
        const email = o.customer_email.toLowerCase().trim();
        const domain = email.split("@")[1] ?? "";
        if (DISPOSABLE_DOMAINS.has(domain)) {
          score += 35;
          reasons.push(`Disposable email domain: ${domain}`);
          flags.disposable_email = true;
        }
        if (PLACEHOLDER_PATTERNS.some((p) => p.test(email.split("@")[0] ?? ""))) {
          score += 15;
          reasons.push("Placeholder email pattern");
          flags.placeholder_email = true;
        }
      }

      // Phone signals
      if (o.customer_phone) {
        const digits = o.customer_phone.replace(/\D/g, "");
        if (digits.length < 9) {
          score += 25;
          reasons.push(`Phone too short: ${digits.length} digits`);
          flags.short_phone = true;
        }
        if (/^(\d)\1+$/.test(digits)) {
          score += 30;
          reasons.push("Phone is all same digit");
          flags.repeated_digits = true;
        }
      } else {
        score += 10;
        reasons.push("No phone provided");
      }

      // Address
      if (o.delivery_address && PLACEHOLDER_PATTERNS.some((p) => p.test(o.delivery_address as string))) {
        score += 20;
        reasons.push("Placeholder-looking address");
        flags.placeholder_address = true;
      }
      if (o.delivery_address && (o.delivery_address as string).trim().length < 8) {
        score += 15;
        reasons.push("Suspiciously short address");
        flags.short_address = true;
      }

      // Customer name
      if (o.customer_name && PLACEHOLDER_PATTERNS.some((p) => p.test(o.customer_name))) {
        score += 15;
        reasons.push("Placeholder customer name");
        flags.placeholder_name = true;
      }

      // Amount anomaly
      if (o.total && o.total > aov * 3) {
        score += 20;
        reasons.push(`Abnormal amount: ${o.total}₴ vs AOV ${Math.round(aov)}₴`);
        flags.abnormal_amount = true;
      }

      // Velocity
      const phoneVel = o.customer_phone ? velocityPhone.get(o.customer_phone) ?? 0 : 0;
      const emailVel = o.customer_email ? velocityEmail.get(o.customer_email.toLowerCase()) ?? 0 : 0;
      if (phoneVel > 3 || emailVel > 3) {
        score += 25;
        reasons.push(`High velocity: ${Math.max(phoneVel, emailVel)} orders/${lookbackHours}h`);
        flags.velocity = Math.max(phoneVel, emailVel);
      }

      // Cold stranger + big bag
      if (!o.user_id && o.total && o.total > aov * 2) {
        score += 15;
        reasons.push("Anonymous + big amount");
        flags.cold_big_bag = true;
      }

      score = Math.min(100, score);
      const level = score >= 60 ? "high" : score >= 30 ? "medium" : "low";

      newSignals.push({
        order_id: o.id,
        risk_score: score,
        risk_level: level,
        flags,
        reasons,
        status: level === "high" ? "needs_review" : "new",
      });

      if (level === "high") {
        adminAlerts.push({ order: o, reasons, score });
        insightRows.push({
          insight_type: "fraud_risk",
          title: `⚠️ Підозріле замовлення: ${o.customer_name} (${score}/100)`,
          description: `Замовлення #${o.id.slice(0, 8)} на ${o.total}₴ має ${reasons.length} fraud-сигналів: ${reasons.slice(0, 3).join("; ")}.`,
          expected_impact: "Перевірити вручну до відправки. Запобігти збитку.",
          confidence: Math.min(0.95, 0.5 + score / 200),
          risk_level: "high",
          affected_layer: "operations",
          status: "new",
          metrics: { order_id: o.id, score, reasons, total: o.total },
        });
      }
    }

    if (newSignals.length) {
      await supabase.from("order_fraud_signals").upsert(newSignals as never, { onConflict: "order_id" });
    }
    if (insightRows.length) {
      await supabase.from("ai_insights").insert(insightRows as never);
    }

    // Admin alerts
    let alertedTo: number[] = [];
    if (adminAlerts.length > 0) {
      const { data: admins } = await supabase.from("user_roles").select("user_id").eq("role", "admin");
      const adminIds = (admins ?? []).map((a) => a.user_id);
      let chatIds: number[] = [];
      if (adminIds.length) {
        const { data: chatRows } = await supabase
          .from("telegram_chat_ids").select("chat_id")
          .in("user_id", adminIds);
        chatIds = (chatRows ?? []).map((r) => Number(r.chat_id)).filter((n) => Number.isFinite(n) && n !== 0);
      }
      const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
      const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY");
      if (LOVABLE_API_KEY && TELEGRAM_API_KEY && chatIds.length) {
        const text = [
          `🚨 <b>Fraud-алерт: ${adminAlerts.length} HIGH-risk замовлення</b>`,
          "",
          ...adminAlerts.slice(0, 5).map((a) =>
            `🔴 ${escapeHtml(a.order.customer_name)} · ${a.order.total}₴ · score ${a.score}\n   ${escapeHtml(a.reasons.slice(0, 2).join("; "))}`
          ),
          "",
          `<a href="https://basic-food.shop/admin/orders">Переглянути в адмінці</a>`,
        ].join("\n");
        const alertResults = await Promise.all(chatIds.map(async (cid) => {
          try {
            const r = await fetch(`${GATEWAY_URL}/sendMessage`, {
              method: "POST",
              headers: {
                Authorization: `Bearer ${LOVABLE_API_KEY}`,
                "X-Connection-Api-Key": TELEGRAM_API_KEY,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({ chat_id: cid, text, parse_mode: "HTML", disable_web_page_preview: true }),
            });
            return r.ok ? cid : null;
          } catch (e) {
            console.error("[fraud-guard] alert send failed", cid, e);
            return null;
          }
        }));
        alertedTo.push(...alertResults.filter((c): c is number => c !== null));
      }
    }

    return new Response(
      JSON.stringify({
        ok: true,
        scanned: orders?.length ?? 0,
        new_signals: newSignals.length,
        high_risk: adminAlerts.length,
        alerted_to: alertedTo,
        aov_baseline: Math.round(aov),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    console.error("[fraud-guard] fatal", e);
    return new Response(JSON.stringify({ error: String((e as Error)?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
