// ACOS Churn Win-back — finds silent customers, but instead of sending the
// blast directly, enqueues a single Tribunal case with the full target list
// + per-customer offer. The judge can downscope (rollout_pct) or reject.
// On `from_tribunal=true` callback we mint promo codes and send Telegram
// messages, respecting any rollout cap from the verdict.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAI } from "../_shared/ai-router.ts";
import { sanitizePlaceholders } from "../_shared/sanitize-placeholders.ts";
import { enqueueTribunalCase } from "../_shared/tribunal.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const TG_TOKEN = Deno.env.get("TELEGRAM_API_KEY");
const SILENT_DAYS = 30;
const COOLDOWN_DAYS = 21;
const PROMO_TTL_HOURS = 72;

type Tier = "vip" | "loyal" | "base";

interface CandidateRow {
  id: string;
  name: string;
  total_orders: number;
  total_spent: number;
  telegram_chat_id: number;
  updated_at: string;
  tags: string[] | null;
}

interface PlannedTarget {
  customer_id: string;
  first_name: string;
  total_orders: number;
  days_silent: number;
  tier: Tier;
  pct: number;
  min_order: number;
  emoji: string;
  label: string;
  chat_id: number;
}

function tierFor(totalOrders: number, totalSpent: number): Tier {
  if (totalOrders >= 5 || totalSpent >= 3000) return "vip";
  if (totalOrders >= 3 || totalSpent >= 1500) return "loyal";
  return "base";
}
function offerFor(tier: Tier): { pct: number; minOrder: number; emoji: string; label: string } {
  if (tier === "vip") return { pct: 20, minOrder: 200, emoji: "👑", label: "VIP" };
  if (tier === "loyal") return { pct: 17, minOrder: 250, emoji: "💎", label: "постійний клієнт" };
  return { pct: 15, minOrder: 300, emoji: "🎁", label: "клієнт" };
}

function generateCode(pct: number, prefix = "WB"): string {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let s = "";
  for (let i = 0; i < 6; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return `${prefix}${pct}-${s}`;
}

async function generateHook(name: string, totalOrders: number, daysSilent: number): Promise<string | null> {
  try {
    const result = await routeAI({
      model: "google/gemini-2.5-flash-lite",
      messages: [
        {
          role: "system",
          content: "Ти — копірайтер бренду BASIC.FOOD (сушене мʼясо для собак та котів). Пиши українською, тепло, без штампів. 1-2 короткі речення (≤180 символів сумарно), 1-2 емоджі. Не вказуй знижку чи промокод — їх додасть шаблон нижче.",
        },
        { role: "user", content: `Імʼя: ${name}. Кількість попередніх замовлень: ${totalOrders}. Не купував(ла) ${daysSilent} днів. Напиши теплий персональний хук для повернення.` },
      ],
      temperature: 0.8,
      max_tokens: 120,
    });
    const text = sanitizePlaceholders(result.content?.trim() ?? "", name);
    return text.length > 10 && text.length < 240 ? text : null;
  } catch { return null; }
}

async function sendTelegram(chatId: number, text: string): Promise<boolean> {
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
    });
    return res.ok;
  } catch { return false; }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  if (!TG_TOKEN) {
    return new Response(JSON.stringify({ error: "TELEGRAM_API_KEY missing" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  try {
    const body = await req.json().catch(() => ({}));

    if (body?.from_tribunal === true) {
      return await runApprovedBlast(body);
    }

    // ── Plan the blast and enqueue a Tribunal case ──
    const cutoff = new Date(Date.now() - SILENT_DAYS * 24 * 60 * 60 * 1000).toISOString();
    const { data: customers, error: cErr } = await supabase
      .from("customers")
      .select("id, name, phone, email, total_orders, total_spent, telegram_chat_id, updated_at, tags")
      .gte("total_orders", 2)
      .lt("updated_at", cutoff)
      .not("telegram_chat_id", "is", null)
      .limit(50);

    if (cErr) throw cErr;
    if (!customers || customers.length === 0) {
      return new Response(JSON.stringify({ targeted: 0, reason: "no candidates" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const cooldownIso = new Date(Date.now() - COOLDOWN_DAYS * 24 * 60 * 60 * 1000).toISOString();
    const PAUSE_DAYS = 14;
    const BLOCK_DAYS = 30;
    const pauseCutoff = Date.now() - PAUSE_DAYS * 24 * 60 * 60 * 1000;
    const blockCutoff = Date.now() - BLOCK_DAYS * 24 * 60 * 60 * 1000;
    const hasFreshTag = (tags: string[] | null | undefined, prefix: string, cutoffMs: number) => {
      if (!tags?.length) return false;
      const tag = tags.find((t) => t.startsWith(prefix));
      if (!tag) return false;
      const ts = new Date(tag.split(":")[1]).getTime();
      return !isNaN(ts) && ts >= cutoffMs;
    };
    const isSkipped = (tags: string[] | null | undefined) =>
      hasFreshTag(tags, "promo_paused:", pauseCutoff) ||
      hasFreshTag(tags, "tg_blocked:", blockCutoff);
    const isChurnRisk = (tags: string[] | null | undefined) =>
      hasFreshTag(tags, "churn_risk:", Date.now() - 30 * 24 * 60 * 60 * 1000);

    const { data: recentWbPromos } = await supabase
      .from("promo_codes")
      .select("code, created_at")
      .like("code", "WB%")
      .gte("created_at", cooldownIso);

    const targetedCustomerIds = new Set<string>();
    if ((recentWbPromos ?? []).length > 0) {
      const { data: recentEvents } = await supabase
        .from("events")
        .select("metadata")
        .eq("event_type", "winback_sent")
        .gte("created_at", cooldownIso)
        .limit(500);
      for (const e of recentEvents ?? []) {
        const cid = (e.metadata as { customer_id?: string })?.customer_id;
        if (cid) targetedCustomerIds.add(cid);
      }
    }

    const candidates = (customers as CandidateRow[])
      .filter((c) => !targetedCustomerIds.has(c.id) && !isSkipped(c.tags))
      .sort((a, b) => Number(isChurnRisk(b.tags)) - Number(isChurnRisk(a.tags)));

    if (candidates.length === 0) {
      return new Response(JSON.stringify({ targeted: 0, reason: "all in cooldown or paused" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const planned: PlannedTarget[] = candidates.map((c) => {
      const tier = tierFor(c.total_orders, c.total_spent);
      const offer = offerFor(tier);
      const daysSilent = Math.round((Date.now() - new Date(c.updated_at).getTime()) / (24 * 60 * 60 * 1000));
      const firstName = (c.name ?? "друже").split(" ")[0];
      return {
        customer_id: c.id,
        first_name: firstName,
        total_orders: c.total_orders,
        days_silent: daysSilent,
        tier,
        pct: offer.pct,
        min_order: offer.minOrder,
        emoji: offer.emoji,
        label: offer.label,
        chat_id: Number(c.telegram_chat_id),
      };
    });

    const tierBreakdown = { vip: 0, loyal: 0, base: 0 };
    for (const p of planned) tierBreakdown[p.tier]++;

    const enq = await enqueueTribunalCase({
      source_function: "acos-winback",
      category: "broadcast",
      urgency: planned.length > 30 ? "high" : "normal",
      proposed_change: {
        kind: "winback_blast",
        ttl_hours: PROMO_TTL_HOURS,
        planned, // full target list
      },
      context: {
        candidates: planned.length,
        tier_breakdown: tierBreakdown,
        silent_days: SILENT_DAYS,
        cooldown_days: COOLDOWN_DAYS,
      },
      expected_impact: `Winback blast to ${planned.length} silent customers (~${Math.round(planned.length * 0.12)} expected orders).`,
    });

    return new Response(
      JSON.stringify({
        queued: true,
        case_id: enq.case_id,
        reused: enq.reused,
        previous_verdict: enq.previous_verdict ?? null,
        candidates: planned.length,
        tier_breakdown: tierBreakdown,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-winback error", err);
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});

async function runApprovedBlast(body: {
  proposed_change?: Record<string, unknown>;
  conditions?: Record<string, unknown>;
  case_id?: string;
}): Promise<Response> {
  const change = body.proposed_change as
    | { planned?: PlannedTarget[]; ttl_hours?: number } | undefined;
  if (!change?.planned?.length) {
    return new Response(
      JSON.stringify({ ok: false, error: "missing_proposed_change" }),
      { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // Honor judge's rollout_pct cap if present.
  const rolloutPct = (body.conditions as { rollout_pct?: number } | undefined)?.rollout_pct;
  let targets = change.planned;
  if (typeof rolloutPct === "number" && rolloutPct > 0 && rolloutPct < 100) {
    const cap = Math.max(1, Math.floor(targets.length * rolloutPct / 100));
    targets = targets.slice(0, cap);
  }

  const ttlHours = change.ttl_hours ?? PROMO_TTL_HOURS;
  const now = Date.now();
  const expiresAt = new Date(now + ttlHours * 60 * 60 * 1000).toISOString();
  const startsAt = new Date(now).toISOString();

  // 1. Batch mint all promo codes.
  const promoRows = targets.map((t) => ({
    code: generateCode(t.pct),
    discount_type: "percent",
    discount_value: t.pct,
    max_uses: 1,
    min_order_amount: t.min_order,
    starts_at: startsAt,
    ends_at: expiresAt,
    is_active: true,
  }));
  const { data: promos, error: batchPromoErr } = await supabase
    .from("promo_codes")
    .insert(promoRows)
    .select("id, code");
  if (batchPromoErr || !promos || promos.length !== targets.length) {
    return new Response(
      JSON.stringify({ ok: false, error: batchPromoErr?.message ?? "promo batch insert mismatch" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // 2. Generate AI hooks in parallel.
  const hooks = await Promise.all(
    targets.map((t) => generateHook(t.first_name, t.total_orders, t.days_silent)),
  );

  // 3. Send Telegrams in parallel.
  type SendResult =
    | { ok: true; target: PlannedTarget; promo: { id: string; code: string }; aiHook: string | null }
    | { ok: false; promoId: string; target: PlannedTarget; promo: { id: string; code: string } };

  const sendResults: SendResult[] = await Promise.all(
    targets.map(async (t, i) => {
      const promo = promos[i];
      const aiHook = hooks[i];
      const intro = aiHook
        ? `${t.emoji} <b>${t.first_name}</b>\n\n${aiHook}`
        : `${t.emoji} <b>${t.first_name}, ми за вами скучили!</b>\n\nВи — наш ${t.label} (${t.total_orders} замовлень). Хочемо повернути вас спеціальною пропозицією.`;
      const msg =
        `${intro}\n\n` +
        `🎫 <b>Персональний промокод:</b>\n<code>${promo.code}</code>\n\n` +
        `💰 Знижка <b>−${t.pct}%</b> на наступне замовлення\n` +
        `🛒 Мінімальне замовлення: ${t.min_order} ₴\n` +
        `⏰ Діє <b>${ttlHours} години</b> (одноразовий)\n\n` +
        `🔗 <a href="https://basic-food.shop/catalog">Обрати ласощі</a>`;
      const ok = await sendTelegram(t.chat_id, msg);
      if (!ok) return { ok: false, promoId: promo.id, target: t, promo };
      return { ok: true, target: t, promo, aiHook };
    }),
  );

  // 4. Deactivate orphaned promos in batch.
  const orphanIds = sendResults
    .filter((r): r is Extract<SendResult, { ok: false }> => !r.ok)
    .map((r) => r.promoId);
  if (orphanIds.length > 0) {
    await supabase.from("promo_codes").update({ is_active: false }).in("id", orphanIds).catch(() => {});
  }

  // 5. Batch insert events for all results (success and failure).
  await supabase.from("events").insert(
    sendResults.map((r) => ({
      event_type: "winback_sent",
      user_id: null,
      metadata: {
        customer_id: r.target.customer_id,
        promo_code: r.promo.code,
        promo_id: r.promo.id,
        success: r.ok,
        days_silent: r.target.days_silent,
        tier: r.target.tier,
        discount_pct: r.target.pct,
        min_order: r.target.min_order,
        ai_hook_used: r.ok ? r.aiHook !== null : false,
        tribunal_case_id: body.case_id,
      },
      source: "acos",
    })),
  ).catch(() => {});

  const sentItems = sendResults.filter((r): r is Extract<SendResult, { ok: true }> => r.ok);
  let sent = sentItems.length;
  const tierBreakdown = { vip: 0, loyal: 0, base: 0 };
  const results: Array<{ customer_id: string; sent: boolean; code: string; tier: Tier }> = [];
  for (const r of sendResults) {
    if (r.ok) tierBreakdown[r.target.tier]++;
    results.push({ customer_id: r.target.customer_id, sent: r.ok, code: r.promo.code, tier: r.target.tier });
  }

  if (sent > 0) {
    await supabase.from("ai_insights").insert({
      insight_type: "winback_campaign",
      title: `Tribunal: Win-back ${sent} клієнтів (rollout ${rolloutPct ?? 100}%)`,
      description: `Tribunal схвалив (case ${body.case_id ?? "?"}). Надіслано ${sent}/${targets.length} персональних промокодів.`,
      expected_impact: `+${Math.round(sent * 0.12)} орд (~${Math.round(sent * 0.12 * 350)}₴ revenue)`,
      confidence: 0.7,
      affected_layer: "telegram_bot",
      risk_level: "low",
      metrics: {
        sent,
        attempted: targets.length,
        tier_breakdown: tierBreakdown,
        ttl_hours: ttlHours,
        rollout_pct: rolloutPct ?? 100,
        tribunal_case_id: body.case_id,
      },
    });
  }

  return new Response(
    JSON.stringify({ ok: true, targeted: sent, attempted: targets.length, tier_breakdown: tierBreakdown, results }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
}
