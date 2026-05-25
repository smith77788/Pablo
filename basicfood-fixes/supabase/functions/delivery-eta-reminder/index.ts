// delivery-eta-reminder — daily cron.
//
// Butternut "Your box arrives tomorrow 🎉" parity. For orders with
// preferred_delivery_date == today + 1, send an in-app notification AND a
// best-effort web push the day before. Only for paid/processing orders that
// haven't been cancelled.
//
// Dedupe via notification (user_id, type='delivery_eta', reference_id=order.id)
// — one nudge per order, ever.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  // "tomorrow" in UTC (cron runs at 17:00 UTC = 19:00/20:00 Kyiv)
  const tomorrow = new Date();
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  const tomorrowDate = tomorrow.toISOString().slice(0, 10);

  const { data: orders, error } = await supabase
    .from("orders")
    .select("id, user_id, customer_name, status")
    .eq("preferred_delivery_date", tomorrowDate)
    .not("user_id", "is", null)
    .not("status", "in", "(cancelled,failed,refunded)")
    .limit(500);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const orderList = (orders ?? []) as Array<{ id: string; user_id: string; customer_name: string | null }>;
  if (orderList.length === 0) {
    return new Response(
      JSON.stringify({ ok: true, scanned: 0, notified: 0, pushed: 0, skipped: 0 }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // Batch dedupe check — single query instead of N count queries
  const orderIds = orderList.map((o) => o.id);
  const { data: existing } = await supabase
    .from("notifications")
    .select("reference_id")
    .eq("type", "delivery_eta")
    .in("reference_id", orderIds);
  const alreadyNotified = new Set((existing ?? []).map((n: any) => n.reference_id as string));

  const toNotify = orderList.filter((o) => !alreadyNotified.has(o.id));
  const skipped = orderList.length - toNotify.length;

  const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
  const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const CRON_SECRET = Deno.env.get("CRON_SECRET") ?? "";
  const title = "Замовлення прибуває завтра 🎉";
  const body = "Підготуйте місце для коробки і смаколики чекатимуть свого героя.";

  // Parallel: insert notification + send push for each qualifying order
  const results = await Promise.all(toNotify.map(async (o) => {
    const { error: nErr } = await supabase.from("notifications").insert({
      user_id: o.user_id,
      type: "delivery_eta",
      title,
      message: body,
      reference_id: o.id,
    });
    if (nErr) return { notified: false, pushed: false };

    let pushOk = false;
    try {
      const pushRes = await fetch(`${SUPABASE_URL}/functions/v1/send-web-push`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-cron-secret": CRON_SECRET,
          Authorization: `Bearer ${SERVICE_ROLE}`,
        },
        body: JSON.stringify({
          user_id: o.user_id,
          title,
          body,
          url: `/profile?tab=orders&order=${o.id}`,
          tag: `delivery-eta-${o.id}`,
          campaign: "delivery_eta",
          reference_id: o.id,
        }),
      });
      pushOk = pushRes.ok;
    } catch {/* ignore */}

    return { notified: true, pushed: pushOk };
  }));

  const notified = results.filter((r) => r.notified).length;
  const pushed = results.filter((r) => r.pushed).length;

  return new Response(
    JSON.stringify({ ok: true, scanned: orderList.length, notified, pushed, skipped }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
