// Розрахунок податків ФОП 3 групи 5%: ЄП за квартал + ЄСВ.
// Створює/оновлює запис у finance_tax_obligations з дедлайнами.
// Дедлайни: ЄП — до 19 числа місяця, наступного за кварталом;
//           ЄСВ — до 19 числа місяця, наступного за кварталом.
// Звітність: декларація ЄП — до 40 днів після кварталу.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

const ESV_MONTHLY_2026 = 1760; // оновити при зміні мінзарплати

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const body = await req.json().catch(() => ({}));
    let { year, quarter } = body;

    if (!year || !quarter) {
      const now = new Date();
      const m = now.getMonth() + 1;
      // recalc previous quarter by default
      quarter = Math.floor((m - 1) / 3) || 4;
      year = quarter === 4 && m <= 3 ? now.getFullYear() - 1 : now.getFullYear();
    }

    const sb = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // Sum FOP book for quarter
    const { data: rows } = await sb
      .from("finance_fop_book")
      .select("amount")
      .eq("year", year)
      .eq("quarter", quarter);
    const income = (rows ?? []).reduce((s, r) => s + Number(r.amount), 0);
    const ep = Math.round(income * 0.05 * 100) / 100;
    const esv = ESV_MONTHLY_2026 * 3;

    // Deadline: 19th of month after quarter
    const monthAfterQ = quarter * 3 + 1;
    const deadlineYear = monthAfterQ > 12 ? year + 1 : year;
    const deadlineMonth = monthAfterQ > 12 ? 1 : monthAfterQ;
    const paymentDeadline = `${deadlineYear}-${String(deadlineMonth).padStart(2, "0")}-19`;
    // Report deadline: 40 days after quarter end
    const qEnd = new Date(year, quarter * 3, 0);
    const reportDeadline = new Date(qEnd);
    reportDeadline.setDate(reportDeadline.getDate() + 40);
    const reportDeadlineStr = reportDeadline.toISOString().slice(0, 10);

    const periodLabel = `Q${quarter} ${year}`;

    const upserts = [
      {
        tax_type: "ep_5pct",
        period_year: year,
        period_quarter: quarter,
        period_label: periodLabel,
        base_amount: income,
        amount_due: ep,
        payment_deadline: paymentDeadline,
        report_deadline: reportDeadlineStr,
      },
      {
        tax_type: "esv",
        period_year: year,
        period_quarter: quarter,
        period_label: periodLabel,
        base_amount: ESV_MONTHLY_2026,
        amount_due: esv,
        payment_deadline: paymentDeadline,
        report_deadline: null,
      },
    ];

    // Parallel selects then single batch upsert for non-paid obligations.
    const existingRows = await Promise.all(
      upserts.map((u) =>
        sb.from("finance_tax_obligations")
          .select("id, status")
          .eq("tax_type", u.tax_type)
          .eq("period_year", u.period_year)
          .eq("period_quarter", u.period_quarter)
          .maybeSingle()
          .then((r) => r.data)
      ),
    );
    const toUpsert = upserts.filter((_, i) => existingRows[i]?.status !== "paid");
    if (toUpsert.length > 0) {
      await sb.from("finance_tax_obligations").upsert(toUpsert, {
        onConflict: "tax_type,period_year,period_quarter",
      });
    }

    return json({ ok: true, year, quarter, income, ep_5pct: ep, esv, total: ep + esv });
  } catch (e) {
    console.error("finance-tax-calculator error", e);
    return json({ error: String((e as Error).message) }, 500);
  }
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
