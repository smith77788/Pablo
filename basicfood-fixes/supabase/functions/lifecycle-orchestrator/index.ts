/**
 * 🌀 Lifecycle Orchestrator
 *
 * Cron: щодня 08:30 UTC (`lifecycle-orchestrator-daily`).
 *
 * Для кожного клієнта рахує days_since_last_order і обирає ОДНУ дію
 * за пріоритетом, дотримуючись deduplication:
 *   - 1 дія на 48 годин на клієнта (`customers.last_lifecycle_action_at`)
 *   - не дублюємо одну й ту саму дію двічі поспіль
 *
 * Дії:
 *   day 3-5   → transition_check_in   (м'який чек-ін)
 *   day 7     → review_request        (якщо ще не залишив відгук)
 *   day cadence-3 → reorder_reminder  (якщо є активний reorder_plan)
 *   day 45    → referral_nudge
 *   day 60+   → winback_handoff       (передає в окремий win-back agent)
 *
 * Dry-run: POST { "dry_run": true } — нічого не пише, лише повертає план.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

const BATCH = 200;
const DEDUP_HOURS = 48;

type ActionKind =
  | "transition_check_in"
  | "review_request"
  | "reorder_reminder"
  | "referral_nudge"
  | "winback_handoff";

interface CustomerRow {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  ref_code: string | null;
  total_orders: number;
  telegram_chat_id: number | null;
  last_lifecycle_action_at: string | null;
  last_lifecycle_action_kind: string | null;
}

interface PlannedAction {
  customer_id: string;
  kind: ActionKind;
  days_since_last_order: number;
  user_id: string | null;
  order_id: string | null;
  telegram_chat_id: number | null;
  title: string;
  message: string;
  cta_url: string;
  cta_label: string;
}

const SITE_URL = "https://basic-food.shop";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const body = await req.clone().json().catch(() => ({} as any));
  const dryRun = body?.dry_run === true;

  return await withAgentRun("lifecycle-orchestrator", detectTrigger(req, body), async () => {
    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const stats = {
      scanned: 0,
      planned: 0,
      executed: 0,
      skipped_dedup: 0,
      by_kind: {} as Record<string, number>,
    };
    const detail: PlannedAction[] = [];

    // Single RPC: returns customer + latest order + has_review + active cadence.
    // Replaces N+1 (was: 1 customers query + N orders + N reviews + N reorder_plans)
    // and avoids the unsafe PostgREST .or() filter on email/phone with special chars.
    const { data: candidates, error: cErr } = await sb.rpc("lifecycle_candidates", {
      _batch: BATCH,
      _dedup_hours: DEDUP_HOURS,
    });
    if (cErr) throw cErr;
    const rows = (candidates ?? []) as Array<{
      customer_id: string;
      customer_name: string;
      customer_email: string | null;
      customer_phone: string | null;
      ref_code: string | null;
      total_orders: number;
      telegram_chat_id: number | null;
      last_lifecycle_action_at: string | null;
      last_lifecycle_action_kind: string | null;
      latest_order_id: string | null;
      latest_order_user_id: string | null;
      latest_order_total: number;
      latest_order_created_at: string | null;
      days_since_last_order: number;
      has_review: boolean;
      active_cadence_days: number | null;
    }>;
    stats.scanned = rows.length;

    for (const r of rows) {
      if (!r.latest_order_id || !r.latest_order_created_at) continue;
      const days = r.days_since_last_order;
      const userId = r.latest_order_user_id;

      let kind: ActionKind | null = null;
      if (days >= 3 && days <= 5) kind = "transition_check_in";
      else if (days === 7 && !r.has_review && (r.latest_order_total ?? 0) >= 200) {
        kind = "review_request";
      } else if (days === 45) kind = "referral_nudge";
      else if (days >= 60) kind = "winback_handoff";
      else if (r.active_cadence_days && days === r.active_cadence_days - 3) {
        kind = "reorder_reminder";
      }

      if (!kind) continue;
      if (r.last_lifecycle_action_kind === kind) {
        stats.skipped_dedup++;
        continue;
      }

      // P1.6: don't re-send winback_handoff if customer is already in winback queue.
      // The dedicated winback-agent will pick them up on Tue.
      if (kind === "winback_handoff" && r.last_lifecycle_action_kind === "winback_handoff") {
        stats.skipped_dedup++;
        continue;
      }

      const c: CustomerRow = {
        id: r.customer_id,
        name: r.customer_name,
        email: r.customer_email,
        phone: r.customer_phone,
        ref_code: r.ref_code,
        total_orders: r.total_orders,
        telegram_chat_id: r.telegram_chat_id,
        last_lifecycle_action_at: r.last_lifecycle_action_at,
        last_lifecycle_action_kind: r.last_lifecycle_action_kind,
      };
      const latest = {
        id: r.latest_order_id,
        user_id: userId,
        total: r.latest_order_total,
        created_at: r.latest_order_created_at,
      };
      const planned = buildAction(c, latest, days, kind);
      detail.push(planned);
      stats.planned++;
      stats.by_kind[kind] = (stats.by_kind[kind] ?? 0) + 1;
    }

    if (dryRun) {
      return {
        result: jsonOk({ ok: true, dry_run: true, stats, detail }),
        summary: `dry-run: planned=${stats.planned} of ${stats.scanned}`,
        payload: { stats, detail },
        status: "success",
      };
    }

    const botToken = Deno.env.get("TELEGRAM_API_KEY");
    const actionNowIso = new Date().toISOString();

    // Execute all entries in parallel — within each entry operations remain sequential.
    await Promise.all(detail.map(async (a) => {
      let delivered = false;

      // 1. In-app notification (if logged-in user)
      if (a.user_id) {
        const { error: nErr } = await sb.from("notifications").insert({
          user_id: a.user_id,
          type: "lifecycle",
          title: a.title,
          message: a.message,
          reference_id: a.customer_id,
        });
        if (!nErr) delivered = true;
      }

      // 2. Telegram fallback / parallel channel
      if (a.telegram_chat_id && botToken) {
        try {
          const tgRes = await fetch(
            `https://api.telegram.org/bot${botToken}/sendMessage`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                chat_id: a.telegram_chat_id,
                text: `*${a.title}*\n\n${a.message}`,
                parse_mode: "Markdown",
                reply_markup: {
                  inline_keyboard: [[{ text: a.cta_label, url: a.cta_url }]],
                },
              }),
            },
          );
          if (tgRes.ok) delivered = true;
        } catch (e) {
          console.warn("[lifecycle] tg send failed", a.customer_id, e);
        }
      }

      if (delivered) {
        await Promise.all([
          sb.from("customers")
            .update({
              last_lifecycle_action_at: actionNowIso,
              last_lifecycle_action_kind: a.kind,
            })
            .eq("id", a.customer_id),
          sb.from("events").insert({
            event_type: "lifecycle_action_sent",
            source: "lifecycle-orchestrator",
            user_id: a.user_id,
            order_id: a.order_id,
            metadata: {
              kind: a.kind,
              days_since_last_order: a.days_since_last_order,
              channels: {
                in_app: !!a.user_id,
                telegram: !!a.telegram_chat_id && !!botToken,
              },
              customer_id: a.customer_id,
            },
          }),
        ]);

        stats.executed++;
      }
    }));

    return {
      result: jsonOk({ ok: true, stats, detail }),
      summary: `lifecycle: scanned=${stats.scanned} executed=${stats.executed} dedup=${stats.skipped_dedup}`,
      payload: { stats, by_kind: stats.by_kind },
      status: stats.executed > 0 ? "success" : "partial",
    };
  }).catch((e) => {
    return new Response(JSON.stringify({ error: String((e as Error)?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  });
});

function buildAction(
  c: CustomerRow,
  latest: any,
  days: number,
  kind: ActionKind,
): PlannedAction {
  const firstName = (c.name || "").split(" ")[0] || "друже";
  const isRepeat = (c.total_orders ?? 0) >= 2;
  let title = "";
  let message = "";
  let ctaUrl = `${SITE_URL}/profile`;
  let ctaLabel = "До профілю";

  switch (kind) {
    case "transition_check_in":
      title = `${firstName}, як справи у вашого улюбленця?`;
      message = `Минуло ${days} днів з вашого замовлення. Якщо є питання щодо переходу на нові ласощі — ми поруч.`;
      ctaUrl = `${SITE_URL}/transition-guide`;
      ctaLabel = "Гайд переходу";
      break;
    case "review_request":
      title = isRepeat ? "Як змінилось за місяць?" : "Поділіться враженнями";
      message = isRepeat
        ? "Ви замовляєте у нас не вперше — розкажіть, як улюбленцю наші ласощі через час?"
        : "Якщо ласощі сподобались, залиште, будь ласка, короткий відгук — це дуже допомагає іншим.";
      ctaUrl = `${SITE_URL}/profile?tab=reviews`;
      ctaLabel = "Залишити відгук";
      break;
    case "reorder_reminder":
      title = "Пора поповнити запас";
      message = "За планом нагадувань — час оформити повторне замовлення. Один клік у профілі.";
      ctaUrl = `${SITE_URL}/reorder/${latest.id}`;
      ctaLabel = "🔁 Повторити";
      break;
    case "referral_nudge":
      title = "Знаєте друга з собакою чи котом?";
      message = c.ref_code
        ? `Ваш улюбленець уже місяць з нами. Ось ваш код «${c.ref_code}» — друг отримає 100₴, ви теж.`
        : "Ваш улюбленець уже місяць з нами. Заходьте в розділ «Реферали» — отримаєте код для друзів.";
      ctaUrl = `${SITE_URL}/profile?tab=referrals`;
      ctaLabel = "Мої реферали";
      break;
    case "winback_handoff":
      title = "Скучили за вами";
      message = `Минуло ${days} днів — ми підготували для вас особливу пропозицію. Загляньте у каталог.`;
      ctaUrl = `${SITE_URL}/catalog`;
      ctaLabel = "До каталогу";
      break;
  }

  return {
    customer_id: c.id,
    kind,
    days_since_last_order: days,
    user_id: latest.user_id ?? null,
    order_id: latest.id ?? null,
    telegram_chat_id: c.telegram_chat_id ?? null,
    title,
    message,
    cta_url: ctaUrl,
    cta_label: ctaLabel,
  };
}

function jsonOk(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
