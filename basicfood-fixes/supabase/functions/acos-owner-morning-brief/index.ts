// ACOS Owner Morning Brief
//
// Once per day (default 09:00 Europe/Kyiv via pg_cron) compiles and pushes a
// Telegram digest to every admin: yesterday's revenue, orders, top product,
// new customers, conversion proxy, top-3 unresolved insights, and biggest
// alert (failed checkout, churn risk, stockout). One screen — owner sees the
// pulse without opening the dashboard.
//
// Idempotency: each run logs an event `morning_brief_sent` with date bucket
// `YYYY-MM-DD`. If a brief already exists for today, the function no-ops
// unless `force: true` is passed.
//
// Manual trigger: POST { force: true } from AdminLaunchCockpit "Send now".

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";

const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const fmtMoney = (n: number) => `${Math.round(n).toLocaleString("uk-UA")} ₴`;
const pct = (a: number, b: number) =>
  b === 0 ? "—" : `${(((a - b) / b) * 100).toFixed(0)}%`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-owner-morning-brief", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const body = await req.json().catch(() => ({}));
    const force = body?.force === true;
    const previewOnly = body?.preview === true;

    // Day buckets (Europe/Kyiv ≈ UTC+2/3 — using UTC for simplicity, owner reads local)
    const now = new Date();
    const todayKey = now.toISOString().slice(0, 10);
    const yStart = new Date(now);
    yStart.setUTCDate(yStart.getUTCDate() - 1);
    yStart.setUTCHours(0, 0, 0, 0);
    const yEnd = new Date(yStart);
    yEnd.setUTCDate(yEnd.getUTCDate() + 1);
    const dayBeforeStart = new Date(yStart);
    dayBeforeStart.setUTCDate(dayBeforeStart.getUTCDate() - 1);

    // ── Idempotency check ──
    if (!force && !previewOnly) {
      const { data: existing } = await supabase
        .from("events")
        .select("id")
        .eq("event_type", "morning_brief_sent")
        .gte("created_at", new Date(now.getTime() - 20 * 3600_000).toISOString())
        .limit(1);
      if (existing && existing.length > 0) {
        return json({ ok: true, action: "noop", reason: "already_sent_today" });
      }
    }

    // ── Yesterday orders (real only by default) ──
    const { data: yOrders } = await supabase
      .from("orders")
      .select("id, total, status, source, customer_name, created_at, message")
      .gte("created_at", yStart.toISOString())
      .lt("created_at", yEnd.toISOString());

    const realY = (yOrders ?? []).filter((o) => o.message !== "seed");
    const ydayRevenue = realY
      .filter((o) => o.status !== "cancelled")
      .reduce((s, o) => s + (o.total ?? 0), 0);
    const ydayCount = realY.length;
    const cancelledCount = realY.filter((o) => o.status === "cancelled").length;
    const aov = ydayCount > 0 ? ydayRevenue / Math.max(1, ydayCount - cancelledCount) : 0;

    // ── Day-before for trend ──
    const { data: dbOrders } = await supabase
      .from("orders")
      .select("total, status, message")
      .gte("created_at", dayBeforeStart.toISOString())
      .lt("created_at", yStart.toISOString());
    const dbReal = (dbOrders ?? []).filter((o) => o.message !== "seed");
    const dbRevenue = dbReal
      .filter((o) => o.status !== "cancelled")
      .reduce((s, o) => s + (o.total ?? 0), 0);
    const dbCount = dbReal.length;

    // ── Top product yesterday ──
    let topProductLine = "—";
    if (realY.length > 0) {
      const orderIds = realY.map((o) => o.id);
      const { data: items } = await supabase
        .from("order_items")
        .select("product_name, quantity, product_price")
        .in("order_id", orderIds);
      const tally = new Map<string, { qty: number; rev: number }>();
      for (const it of items ?? []) {
        const cur = tally.get(it.product_name) ?? { qty: 0, rev: 0 };
        cur.qty += it.quantity ?? 0;
        cur.rev += (it.product_price ?? 0) * (it.quantity ?? 0);
        tally.set(it.product_name, cur);
      }
      const top = [...tally.entries()].sort((a, b) => b[1].rev - a[1].rev)[0];
      if (top) {
        topProductLine = `${escapeHtml(top[0])} — ${top[1].qty} шт, ${fmtMoney(top[1].rev)}`;
      }
    }

    // ── New customers yesterday (real) ──
    const { data: newCusts } = await supabase
      .from("customers")
      .select("id, name, tags, created_at")
      .gte("created_at", yStart.toISOString())
      .lt("created_at", yEnd.toISOString());
    const realNewCusts = (newCusts ?? []).filter((c) => !(c.tags ?? []).includes("seed"));

    // ── Conversion proxy: yesterday begin_checkout vs purchases ──
    const { data: checkoutEvents } = await supabase
      .from("events")
      .select("event_type")
      .in("event_type", ["begin_checkout", "purchase_completed", "page_viewed"])
      .gte("created_at", yStart.toISOString())
      .lt("created_at", yEnd.toISOString());
    const evCounts = (checkoutEvents ?? []).reduce<Record<string, number>>((acc, e) => {
      acc[e.event_type] = (acc[e.event_type] ?? 0) + 1;
      return acc;
    }, {});
    const checkoutStarts = evCounts["begin_checkout"] ?? 0;
    const purchases = evCounts["purchase_completed"] ?? 0;
    const checkoutCvr = checkoutStarts > 0 ? ((purchases / checkoutStarts) * 100).toFixed(1) : "—";

    // ── Top 3 unresolved insights (high confidence, not closed) ──
    const { data: insights } = await supabase
      .from("ai_insights")
      .select("title, description, risk_level, confidence, expected_impact")
      .eq("status", "new")
      .gte("confidence", 0.6)
      .order("confidence", { ascending: false })
      .order("created_at", { ascending: false })
      .limit(3);

    // ── Biggest alert: low stock or churn risk ──
    const { data: lowStock } = await supabase
      .from("products")
      .select("name, stock_quantity")
      .eq("is_active", true)
      .lte("stock_quantity", 20)
      .order("stock_quantity", { ascending: true })
      .limit(3);

    // ── Anti-fraud last 24h ──
    const since24h = new Date(now.getTime() - 24 * 3600_000).toISOString();
    const { data: fraudRows } = await supabase
      .from("order_fraud_signals")
      .select("risk_level, status")
      .gte("created_at", since24h);
    const fraudHigh = (fraudRows ?? []).filter(
      (r) => r.risk_level === "high" && (r.status === "new" || r.status === "review"),
    ).length;
    const fraudMedium = (fraudRows ?? []).filter(
      (r) => r.risk_level === "medium" && (r.status === "new" || r.status === "review"),
    ).length;

    // ── Cart recovery last 24h ──
    const { data: recoveryRows } = await supabase
      .from("cart_recovery_attempts")
      .select("status, recovered_value, cart_value")
      .gte("created_at", since24h);
    const sentCount = recoveryRows?.length ?? 0;
    const recoveredCount = (recoveryRows ?? []).filter((r) => r.status === "recovered").length;
    const recoveredValue = (recoveryRows ?? [])
      .filter((r) => r.status === "recovered")
      .reduce((s, r) => s + (r.recovered_value ?? 0), 0);
    const recoveryRate = sentCount > 0 ? ((recoveredCount / sentCount) * 100).toFixed(0) : "—";

    // ── Pricing actions last 24h ──
    const { data: pricingRows } = await supabase
      .from("pricing_decisions")
      .select("decision, old_price, new_price")
      .gte("created_at", since24h);
    const appliedPricing = (pricingRows ?? []).filter((p) => p.decision === "applied").length;
    const queuedPricing = (pricingRows ?? []).filter((p) => p.decision === "queued").length;

    // ── Compose message ──
    const dateLabel = yStart.toLocaleDateString("uk-UA", {
      day: "2-digit",
      month: "long",
      weekday: "long",
    });

    const lines: string[] = [
      `☀️ <b>Morning Brief — ${escapeHtml(dateLabel)}</b>`,
      "",
      `💰 Виручка: <b>${fmtMoney(ydayRevenue)}</b> ${
        dbRevenue > 0 ? `(${pct(ydayRevenue, dbRevenue)} vs позавчора)` : ""
      }`,
      `📦 Замовлень: <b>${ydayCount}</b>${cancelledCount > 0 ? ` (відмін: ${cancelledCount})` : ""} ${
        dbCount > 0 ? `· позавчора ${dbCount}` : ""
      }`,
      `🧾 AOV: <b>${fmtMoney(aov)}</b>`,
      `🛒 Чекаут CVR: <b>${checkoutCvr}${checkoutCvr === "—" ? "" : "%"}</b> (${checkoutStarts}→${purchases})`,
      `🌱 Нових клієнтів: <b>${realNewCusts.length}</b>`,
      `🏆 Топ-продукт: ${topProductLine}`,
    ];

    if (insights && insights.length > 0) {
      lines.push("", "🧠 <b>Що потребує уваги:</b>");
      for (const i of insights) {
        const risk = i.risk_level === "high" ? "🔴" : i.risk_level === "medium" ? "🟡" : "🟢";
        lines.push(`${risk} ${escapeHtml(i.title)}`);
      }
    }

    if (lowStock && lowStock.length > 0) {
      lines.push("", "📉 <b>Низький залишок:</b>");
      for (const p of lowStock) {
        lines.push(`• ${escapeHtml(p.name)} — ${p.stock_quantity} шт`);
      }
    }

    // Anti-fraud / cart recovery / pricing — only show if something happened
    const opsLines: string[] = [];
    if (fraudHigh > 0 || fraudMedium > 0) {
      opsLines.push(
        `🛡 Фрод (24h): ${fraudHigh > 0 ? `<b>${fraudHigh} high</b>` : ""}${
          fraudHigh > 0 && fraudMedium > 0 ? " · " : ""
        }${fraudMedium > 0 ? `${fraudMedium} medium` : ""} — потребує review`,
      );
    }
    if (sentCount > 0) {
      opsLines.push(
        `🛒 Cart recovery: ${recoveredCount}/${sentCount} (${recoveryRate}%)${
          recoveredValue > 0 ? ` · повернуто ${fmtMoney(recoveredValue)}` : ""
        }`,
      );
    }
    if (appliedPricing > 0 || queuedPricing > 0) {
      opsLines.push(
        `💲 Auto-pricing: ${appliedPricing} застосовано${
          queuedPricing > 0 ? `, ${queuedPricing} у черзі на review` : ""
        }`,
      );
    }
    if (opsLines.length > 0) {
      lines.push("", "⚙️ <b>Operations 24h:</b>", ...opsLines);
    }

    // ── Neural network state: aggregate debug_reports from autonomous agents ──
    // Surfaces what the sensor neurons learned overnight so the owner sees the
    // organism's findings, not just transactions.
    const { data: agentReports } = await supabase
      .from("debug_reports")
      .select("source, level, message, context, last_seen_at")
      .in("source", [
        "abandoned-checkout-recoverer",
        "product-conversion-analyzer",
        "funnel-dropoff-analyzer",
        "brand-compliance-sentinel",
      ])
      .gte("last_seen_at", since24h)
      .order("last_seen_at", { ascending: false });

    const neuralLines: string[] = [];
    const seenSources = new Set<string>();
    for (const r of agentReports ?? []) {
      if (seenSources.has(r.source)) continue; // one (latest) per source
      seenSources.add(r.source);
      const ctx = (r.context ?? {}) as any;
      const icon = r.level === "error" ? "🔴" : r.level === "warn" ? "🟡" : "🟢";

      if (r.source === "abandoned-checkout-recoverer") {
        const total = ctx.abandoned_sessions_total ?? 0;
        const reachable = ctx.reachable_users_total ?? 0;
        if (total > 0) {
          neuralLines.push(`${icon} Покинуті checkout: <b>${total}</b> (досяжних: ${reachable})`);
        }
      } else if (r.source === "product-conversion-analyzer") {
        const suspects = Array.isArray(ctx.suspects) ? ctx.suspects.length : 0;
        if (suspects > 0) {
          neuralLines.push(`${icon} Низька конверсія: <b>${suspects}</b> товарів потребують уваги`);
        }
      } else if (r.source === "funnel-dropoff-analyzer") {
        const bn = ctx.primary_bottleneck;
        if (bn?.field) {
          neuralLines.push(`${icon} Воронка: вузьке місце — <b>${escapeHtml(String(bn.field))}</b> (${bn.count})`);
        }
      } else if (r.source === "brand-compliance-sentinel") {
        const hits = Array.isArray(ctx.hits) ? ctx.hits.length : 0;
        if (hits > 0) {
          neuralLines.push(`${icon} Brand drift: <b>${hits}</b> порушень формулювань`);
        }
      }
    }
    if (neuralLines.length > 0) {
      lines.push("", "🧬 <b>Стан мережі агентів:</b>", ...neuralLines);
    }

    if (ydayCount === 0) {
      lines.push("", "🌑 <i>Жодного реального замовлення вчора. Перевір трафік + промо.</i>");
    }

    lines.push("", `<a href="https://basic-food.shop/admin/launch">📊 Адмінка</a>`);

    const text = lines.join("\n");

    if (previewOnly) {
      return json({
        ok: true,
        action: "preview",
        text,
        preview: text,
        metrics: {
          ydayRevenue,
          ydayCount,
          aov,
          checkoutCvr,
          newCustomers: realNewCusts.length,
          insightCount: insights?.length ?? 0,
          lowStockCount: lowStock?.length ?? 0,
          fraudHigh,
          fraudMedium,
          recoverySent: sentCount,
          recoveryRecovered: recoveredCount,
          recoveryValue: recoveredValue,
          pricingApplied: appliedPricing,
          pricingQueued: queuedPricing,
        },
      });
    }

    // ── Resolve admin chat ids ──
    const { data: admins } = await supabase
      .from("user_roles")
      .select("user_id")
      .eq("role", "admin");
    const adminUserIds = (admins ?? []).map((a) => a.user_id);

    let chatIds: number[] = [];
    if (adminUserIds.length > 0) {
      const { data: profiles } = await supabase
        .from("profiles")
        .select("telegram_chat_id")
        .in("user_id", adminUserIds)
        .not("telegram_chat_id", "is", null);
      chatIds = (profiles ?? [])
        .map((p) => Number(p.telegram_chat_id))
        .filter((n) => Number.isFinite(n) && n !== 0);
    }
    if (chatIds.length === 0) {
      const { data: ownerCust } = await supabase
        .from("customers")
        .select("telegram_chat_id")
        .contains("tags", ["owner"])
        .not("telegram_chat_id", "is", null)
        .limit(3);
      chatIds = (ownerCust ?? [])
        .map((c) => Number(c.telegram_chat_id))
        .filter((n) => Number.isFinite(n) && n !== 0);
    }

    // ── Send via Lovable connector gateway ──
    const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
    const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY");
    const sentTo: number[] = [];

    if (LOVABLE_API_KEY && TELEGRAM_API_KEY && chatIds.length > 0) {
      const results = await Promise.all(chatIds.map(async (cid) => {
        try {
          const r = await fetch(`${GATEWAY_URL}/sendMessage`, {
            method: "POST",
            headers: {
              Authorization: `Bearer ${LOVABLE_API_KEY}`,
              "X-Connection-Api-Key": TELEGRAM_API_KEY,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              chat_id: cid,
              text,
              parse_mode: "HTML",
              disable_web_page_preview: true,
            }),
          });
          return r.ok ? cid : null;
        } catch (e) {
          console.error("[morning-brief] send failed", cid, e);
          return null;
        }
      }));
      sentTo.push(...results.filter((r): r is number => r !== null));
    }

    // ── Log event for idempotency + analytics ──
    await supabase.from("events").insert({
      event_type: "morning_brief_sent",
      source: "system",
      metadata: {
        date: todayKey,
        recipients: sentTo,
        recipient_count: sentTo.length,
        revenue: ydayRevenue,
        order_count: ydayCount,
        forced: force,
      },
    });

    return json({
      ok: true,
      action: sentTo.length > 0 ? "sent" : "no_recipients",
      sent_to: sentTo,
      preview: text,
    });
  } catch (e) {
    console.error("[morning-brief] fatal", e);
    return new Response(
      JSON.stringify({ error: String((e as Error)?.message ?? e) }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
    })();
    return { response: __res };
  });
});

const json = (payload: unknown, status = 200) =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
