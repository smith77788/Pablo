/**
 * AI Weekly Digest
 *
 * Cron: щонеділі о 18:00 UTC.
 *
 * Збирає за 7 днів:
 *  - усі ai_actions (applied / measured / reverted)
 *  - топ-5 patterns у ai_memory за impact
 *  - кількість токсичних патернів
 *  - усі insights високого ризику
 *
 * Відправляє адмінам compact HTML-зведення у Telegram.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

async function tg(token: string, chat: string, text: string): Promise<boolean> {
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chat,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    });
    return r.ok;
  } catch (e) {
    console.warn("[weekly-digest] telegram failed:", String((e as Error)?.message ?? e));
    return false;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const since = new Date(Date.now() - WEEK_MS).toISOString();

    // Actions stats
    const { data: actions } = await sb
      .from("ai_actions")
      .select("status, agent_id, action_type, actual_result")
      .gte("created_at", since)
      .limit(2000);

    const totalActions = actions?.length ?? 0;
    const applied = (actions ?? []).filter((a) => a.status === "applied").length;
    const measured = (actions ?? []).filter((a) => a.status === "measured");
    const reverted = (actions ?? []).filter((a) => a.status === "reverted").length;
    const positive = measured.filter((a) => Number(a.actual_result?.delta ?? 0) > 0).length;
    const negative = measured.filter((a) => Number(a.actual_result?.delta ?? 0) < 0).length;
    const sumDelta = measured.reduce((s, a) => s + Number(a.actual_result?.delta ?? 0), 0);

    // Top patterns by avg_impact
    const { data: topPatterns } = await sb
      .from("ai_memory")
      .select("pattern_key, learned_rule, success_count, failure_count, avg_impact, is_active")
      .eq("category", "action-effectiveness")
      .order("avg_impact", { ascending: false })
      .limit(5);

    // Toxic count
    const { data: toxicAll } = await sb
      .from("ai_memory")
      .select("pattern_key, success_count, failure_count")
      .eq("category", "action-effectiveness")
      .eq("is_active", false);
    const toxicCount = (toxicAll ?? []).filter((r) => {
      const t = (r.success_count as number) + (r.failure_count as number);
      const rate = t > 0 ? (r.success_count as number) / t : 0;
      return t >= 5 && rate < 0.3;
    }).length;

    // High-risk insights
    const { data: insights } = await sb
      .from("ai_insights")
      .select("title, risk_level")
      .gte("created_at", since)
      .in("risk_level", ["high", "critical"])
      .limit(10);
    const highRiskCount = insights?.length ?? 0;

    // Revenue
    const { data: orders } = await sb
      .from("orders")
      .select("total")
      .gte("created_at", since)
      .neq("status", "cancelled")
      .limit(5000);
    const revenue = (orders ?? []).reduce((s, o) => s + ((o.total as number) || 0), 0);
    const orderCount = orders?.length ?? 0;

    // Compose message
    const lines: string[] = [];
    lines.push(`📊 <b>BasicFood — тижневий AI-звіт</b>`);
    lines.push(`<i>${new Date(since).toLocaleDateString("uk-UA")} – ${new Date().toLocaleDateString("uk-UA")}</i>`);
    lines.push("");
    lines.push(`💰 <b>Бізнес:</b> ${revenue.toFixed(0)} ₴ • ${orderCount} замовлень`);
    lines.push("");
    lines.push(`🤖 <b>AI-дії:</b> ${totalActions} створено`);
    lines.push(`  • applied: ${applied}`);
    lines.push(`  • measured: ${measured.length} (${positive}↑ / ${negative}↓)`);
    lines.push(`  • reverted: ${reverted}`);
    if (measured.length > 0) {
      const avg = (sumDelta / measured.length) * 100;
      lines.push(`  • середній impact: ${avg >= 0 ? "+" : ""}${avg.toFixed(1)}%`);
    }
    lines.push("");
    lines.push(`🧠 <b>Memory:</b> ${toxicCount} токсичних патернів вимкнено`);
    if (highRiskCount > 0) lines.push(`⚠️ <b>${highRiskCount}</b> insights високого ризику за тиждень`);

    if ((topPatterns ?? []).length > 0) {
      lines.push("");
      lines.push(`<b>🏆 Топ patterns за impact:</b>`);
      for (const p of (topPatterns ?? []).slice(0, 5)) {
        const t = (p.success_count as number) + (p.failure_count as number);
        const rate = t > 0 ? Math.round(((p.success_count as number) / t) * 100) : 0;
        const action = (p.pattern_key as string).split(":").slice(2).join(":");
        const status = p.is_active ? "✅" : "❌";
        lines.push(`  ${status} <code>${action}</code>: ${rate}% (n=${t}, impact ${Number(p.avg_impact).toFixed(2)})`);
      }
    }

    lines.push("");
    lines.push(`<a href="https://basic-food.shop/admin/ai-memory">Відкрити AI Memory →</a>`);

    const text = lines.join("\n");

    // Send
    const token = Deno.env.get("TELEGRAM_API_KEY_1");
    if (!token) {
      return new Response(JSON.stringify({ ok: true, dry_run: true, text }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // resolve admin chat ids
    const { data: admins } = await sb.from("user_roles").select("user_id").eq("role", "admin");
    const adminIds = (admins ?? []).map((a) => a.user_id as string);
    let chats: number[] = [];
    if (adminIds.length > 0) {
      // INC-0003 hotfix: customers has no user_id. Use telegram_chat_ids.
      const { data } = await sb
        .from("telegram_chat_ids")
        .select("chat_id")
        .in("user_id", adminIds)
        .not("chat_id", "is", null);
      chats = ((data ?? []) as any[])
        .map((c: any) => (c.chat_id == null ? null : Number(c.chat_id)))
        .filter((x): x is number => x !== null);
    }
    if (chats.length === 0) {
      const fb = Deno.env.get("TELEGRAM_ADMIN_CHAT_ID");
      if (fb) chats = [Number(fb)];
    }

    let sent = 0;
    for (const c of chats) {
      const ok = await tg(token, String(c), text);
      if (ok) sent++;
    }

    return new Response(
      JSON.stringify({ ok: true, sent, total_chats: chats.length, text_preview: text.slice(0, 500) }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("ai-weekly-digest error", err);
    return new Response(
      JSON.stringify({ ok: false, error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
