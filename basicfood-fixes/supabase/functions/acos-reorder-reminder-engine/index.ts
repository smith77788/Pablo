// acos-reorder-reminder-engine
//
// Cron-driven retention engine. Runs daily.
//
// Logic:
//   1. Find all delivered/completed orders 14 days old (±1 day window)
//      where the user has NOT placed a follow-up order since
//   2. Skip if user already received a reminder for this order
//   3. Send push deep-linked to /reorder/<order_id>
//   4. Log reorder_reminder_sent event for ACOS measurement loop
//
// Why 14 days: matches consumption cycle for 100-200g treat packs
// (~1 pack per pet per fortnight). Tunable via REMINDER_DAYS query param.
//
// Idempotency: dedupe by checking events for prior reminder per order.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  const url = new URL(req.url);
  const reminderDays = parseInt(url.searchParams.get("days") ?? "14", 10);
  const dryRun = url.searchParams.get("dry_run") === "1";

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const windowStart = new Date(Date.now() - (reminderDays + 1) * 24 * 60 * 60 * 1000);
  const windowEnd = new Date(Date.now() - (reminderDays - 1) * 24 * 60 * 60 * 1000);

  // 1. Candidate orders: delivered/completed, within window, has user_id
  const { data: candidates, error } = await supabase
    .from("orders")
    .select("id, user_id, customer_name, total, created_at, status")
    .not("user_id", "is", null)
    .in("status", ["delivered", "completed", "paid", "shipped"])
    .gte("created_at", windowStart.toISOString())
    .lt("created_at", windowEnd.toISOString());

  if (error) {
    console.error("[reorder-reminder] fetch failed", error);
    return new Response(JSON.stringify({ ok: false, error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (!candidates?.length) {
    return new Response(
      JSON.stringify({ ok: true, candidates: 0, sent: 0, skipped: 0 }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // ---- BATCH PREFETCH (replaces 3×N per-order DB queries) ----
  const orderIds = candidates.map((c) => c.id);
  const userIds = [...new Set(candidates.map((c) => c.user_id as string))];

  const [recentOrdersRes, remindersRes, tokensRes] = await Promise.all([
    supabase.from("orders").select("user_id, created_at").in("user_id", userIds).gt("created_at", windowStart.toISOString()),
    supabase.from("events").select("order_id").eq("event_type", "reorder_reminder_sent").in("order_id", orderIds),
    supabase.from("device_tokens").select("user_id").in("user_id", userIds).eq("is_active", true),
  ]);

  // Build lookup maps
  const recentByUser = new Map<string, string[]>();
  for (const o of (recentOrdersRes.data ?? []) as { user_id: string; created_at: string }[]) {
    const arr = recentByUser.get(o.user_id) ?? [];
    arr.push(o.created_at);
    recentByUser.set(o.user_id, arr);
  }
  const alreadyReminded = new Set((remindersRes.data ?? []).map((e: any) => e.order_id as string));
  const usersWithTokens = new Set((tokensRes.data ?? []).map((t: any) => t.user_id as string));

  // ---- IN-MEMORY FILTER ----
  let sent = 0;
  let skipped = 0;
  const skipReasons: Record<string, number> = {};
  const bump = (k: string) => (skipReasons[k] = (skipReasons[k] ?? 0) + 1);
  const toSend: { id: string; user_id: string; customer_name: string | null; created_at: string }[] = [];

  for (const order of candidates) {
    if (!order.user_id) { skipped++; bump("no_user"); continue; }

    // Check reordered
    if ((recentByUser.get(order.user_id) ?? []).some((t) => t > order.created_at)) {
      skipped++; bump("already_reordered"); continue;
    }

    // Check prior reminder
    if (alreadyReminded.has(order.id)) { skipped++; bump("already_reminded"); continue; }

    // Check device tokens
    if (!usersWithTokens.has(order.user_id)) { skipped++; bump("no_device_tokens"); continue; }

    if (dryRun) { sent++; continue; }
    toSend.push(order as any);
  }

  // ---- PARALLEL PUSH SENDS ----
  const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
  const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

  const sendResults = await Promise.all(toSend.map(async (order) => {
    const firstName = (order.customer_name ?? "").split(" ")[0] || "Друже";
    const pushBody = {
      user_id: order.user_id,
      title: `${firstName}, час поповнити запас 🐾`,
      body: "Ваш улюбленець, мабуть, уже доїдає попередні ласощі. Один тап — і повторюємо замовлення.",
      data: { url: `/reorder/${order.id}`, campaign: "reorder_reminder_d14", order_id: order.id },
      campaign: "reorder_reminder_d14",
      reference_id: order.id,
    };
    try {
      const res = await fetch(`${SUPABASE_URL}/functions/v1/send-push`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${SERVICE_ROLE}` },
        body: JSON.stringify(pushBody),
      });
      return res.ok ? order : null;
    } catch (e) {
      console.warn("[reorder-reminder] send-push failed", order.id, e);
      return null;
    }
  }));

  const succeeded = sendResults.filter((r): r is typeof toSend[number] => r !== null);
  sent = succeeded.length;
  skipped += toSend.length - sent;
  for (let i = 0; i < toSend.length - sent; i++) bump("send_failed");

  // ---- BATCH EVENT INSERT ----
  if (succeeded.length > 0) {
    await supabase.from("events").insert(
      succeeded.map((order) => ({
        event_type: "reorder_reminder_sent",
        source: "acos-reorder-reminder-engine",
        user_id: order.user_id,
        order_id: order.id,
        metadata: { campaign: "reorder_reminder_d14", reminder_days: reminderDays },
      })),
    );
  }

  return new Response(
    JSON.stringify({
      ok: true,
      candidates: candidates.length,
      sent,
      skipped,
      skip_reasons: skipReasons,
      window: { start: windowStart, end: windowEnd, days: reminderDays },
      dry_run: dryRun,
    }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
