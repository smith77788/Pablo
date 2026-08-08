// Cron: щодня перевіряє finance_tax_obligations, шле адмін-нотифікацію
// за 5 / 1 день до дедлайну сплати або звітування.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const in5 = new Date(today); in5.setDate(in5.getDate() + 5);

    // Pre-fetch admin/moderator IDs and due obligations in parallel.
    const [{ data: due }, { data: adminRoles }] = await Promise.all([
      sb.from("finance_tax_obligations")
        .select("*")
        .eq("status", "pending")
        .lte("payment_deadline", in5.toISOString().slice(0, 10)),
      sb.from("user_roles")
        .select("user_id")
        .in("role", ["admin", "moderator"]),
    ]);
    const adminUserIds: string[] = (adminRoles ?? []).map((r: any) => r.user_id as string);

    let sent = 0;
    const notificationRows: object[] = [];
    const overdueIds: string[] = [];
    const notifiedIds: string[] = [];

    for (const t of due ?? []) {
      // Skip if already notified in last 24h
      if (t.notification_sent_at) {
        const age = Date.now() - new Date(t.notification_sent_at).getTime();
        if (age < 24 * 3600 * 1000) continue;
      }

      const deadline = new Date(t.payment_deadline);
      deadline.setHours(0, 0, 0, 0);
      const days = Math.round((deadline.getTime() - today.getTime()) / (24 * 3600 * 1000));
      const overdue = days < 0;

      const taxName = t.tax_type === "ep_5pct"
        ? "Єдиний податок 5%"
        : t.tax_type === "esv" ? "ЄСВ" : "Податок";

      const msg = overdue
        ? `⚠️ ПРОСТРОЧЕНО: ${taxName} за ${t.period_label}, ${t.amount_due} грн (дедлайн був ${t.payment_deadline})`
        : `📅 За ${days} ${days === 1 ? "день" : "днів"}: ${taxName} ${t.period_label}, ${t.amount_due} грн (до ${t.payment_deadline})`;

      // Collect notification rows for batch insert — one per admin user.
      for (const userId of adminUserIds) {
        notificationRows.push({
          user_id: userId,
          type: "tax_reminder",
          title: "Нагадування про податки",
          message: msg,
          reference_id: t.id,
        });
      }

      if (overdue) overdueIds.push(t.id);
      notifiedIds.push(t.id);
      sent++;
    }

    // Batch all writes after the loop.
    const notifAt = new Date().toISOString();
    await Promise.all([
      notificationRows.length > 0
        ? sb.from("notifications").insert(notificationRows)
        : Promise.resolve(),
      overdueIds.length > 0
        ? sb.from("finance_tax_obligations").update({ status: "overdue" }).in("id", overdueIds)
        : Promise.resolve(),
      notifiedIds.length > 0
        ? sb.from("finance_tax_obligations").update({ notification_sent_at: notifAt }).in("id", notifiedIds)
        : Promise.resolve(),
    ]);

    return json({ ok: true, sent, total: (due ?? []).length });
  } catch (e) {
    console.error("finance-tax-reminder error", e);
    return json({ error: String((e as Error).message) }, 500);
  }
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
