// post-purchase-nurture
//
// Daily cron. For each non-cancelled order, sends three lifecycle nudges:
//   Day 1  → "Як проходить переход на сушені ласощі?" (transition tips)
//   Day 7  → "Час оцінити" + link to leave a review
//   Day 14 → "Готові повторити?" + 10% promo NURTURE<orderId8>
// In-app notification + best-effort web push. Idempotent via per-order/per-stage
// notification lookback. Only triggers for orders.user_id NOT NULL.
//
// Mirrors reorder-reminder/pet-birthday-reminder patterns: requireInternalCaller
// gate, service-role client, x-cron-secret on push invoke.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

type Stage = {
  key: "post_purchase_d1" | "post_purchase_d7" | "post_purchase_d14";
  daysMin: number;
  daysMax: number;
  title: (orderShort: string) => string;
  message: (orderShort: string, code?: string) => string;
  pushTitle: () => string;
  pushBody: (code?: string) => string;
  url: (orderId: string, code?: string) => string;
  needsPromo?: boolean;
};

const STAGES: Stage[] = [
  {
    key: "post_purchase_d1",
    daysMin: 1,
    daysMax: 2,
    title: () => "Як проходить переход на сушені ласощі?",
    message: () =>
      "Перші дні — найважливіші. Вводьте новий смак поступово: 1-2 шматочки на день, спостерігайте за реакцією. Більше порад у профілі улюбленця.",
    pushTitle: () => "BASIC.FOOD — поради після покупки",
    pushBody: () => "Як правильно ввести нові ласощі — короткий гайд",
    url: () => `/profile?tab=pets`,
  },
  {
    key: "post_purchase_d7",
    daysMin: 7,
    daysMax: 9,
    title: (s) => `Як вам замовлення #${s}?`,
    message: () =>
      "Вашій думці довіряють інші власники. Залиште короткий відгук — це займе 30 секунд і допоможе нам стати кращими.",
    pushTitle: () => "Поділіться враженням",
    pushBody: () => "30 секунд — і відгук готовий",
    url: () => `/profile?tab=orders`,
  },
  {
    key: "post_purchase_d14",
    daysMin: 14,
    daysMax: 16,
    needsPromo: true,
    title: () => "Час поповнити запас?",
    message: (_s, code) =>
      `Минуло два тижні. Якщо ласощі вже на завершенні — тримайте промокод ${code} на 10% знижки (діє 7 днів).`,
    pushTitle: () => "Час повторити замовлення",
    pushBody: (code) => `Промокод ${code} — 10% знижки на наступний бокс`,
    url: (_o, code) => `/cart?promo=${code}`,
  },
];

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const now = Date.now();
  const summary: Record<string, number> = {};

  for (const stage of STAGES) {
    const minIso = new Date(now - stage.daysMax * 86400_000).toISOString();
    const maxIso = new Date(now - stage.daysMin * 86400_000).toISOString();

    const { data: orders, error } = await supabase
      .from("orders")
      .select("id, user_id, created_at")
      .not("user_id", "is", null)
      .neq("status", "cancelled")
      .gte("created_at", minIso)
      .lte("created_at", maxIso)
      .limit(500);

    if (error) {
      summary[`${stage.key}_error`] = 1;
      continue;
    }

    // Batch dedupe check — single query instead of N count queries
    const stageOrderIds = (orders ?? []).map((o: any) => o.id);
    const { data: existingNotifs } = stageOrderIds.length > 0
      ? await supabase.from("notifications").select("reference_id").eq("type", stage.key).in("reference_id", stageOrderIds)
      : { data: [] };
    const alreadySent = new Set((existingNotifs ?? []).map((n: any) => n.reference_id as string));
    const toProcess = (orders ?? []).filter((o: any) => !alreadySent.has(o.id));

    const CRON_SECRET = Deno.env.get("CRON_SECRET") ?? "";
    const expiresAt = new Date(now + 7 * 86400_000).toISOString();

    // Parallel processing across qualifying orders
    const stageResults = await Promise.all(toProcess.map(async (o: any) => {
      const orderShort = (o.id as string).slice(0, 8).toUpperCase();
      let code: string | undefined;

      if (stage.needsPromo) {
        code = `NURTURE${orderShort}`;
        await supabase.from("promo_codes").upsert(
          { code, discount_type: "percentage", discount_value: 10, min_order_amount: 300, max_uses: 1, ends_at: expiresAt, is_active: true },
          { onConflict: "code" },
        );
      }

      const { error: notifErr } = await supabase.from("notifications").insert({
        user_id: o.user_id,
        type: stage.key,
        title: stage.title(orderShort),
        message: stage.message(orderShort, code),
        reference_id: o.id,
      });
      if (notifErr) return false;

      try {
        await supabase.functions.invoke("send-web-push", {
          body: {
            user_id: o.user_id,
            title: stage.pushTitle(),
            body: stage.pushBody(code),
            url: stage.url(o.id, code),
            tag: `${stage.key}-${o.id}`,
            campaign: stage.key,
            reference_id: o.id,
          },
          headers: { "x-cron-secret": CRON_SECRET },
        });
      } catch {/* push optional */}
      return true;
    }));
    summary[stage.key] = stageResults.filter(Boolean).length;
  }

  return new Response(
    JSON.stringify({ ok: true, summary }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
