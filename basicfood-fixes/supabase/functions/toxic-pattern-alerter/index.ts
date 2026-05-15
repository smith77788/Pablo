/**
 * Toxic Pattern Alerter
 *
 * Cron: кожні 2 години.
 *
 * Знаходить нещодавно деактивовані "токсичні" патерни в ai_memory
 * (success_rate < 30%, n ≥ 5) і шле адмінам Telegram-нотифікацію.
 *
 * Дедуплікація: для кожного pattern_key шле alert не частіше за раз на 24h
 * (записує мітку у evidence.last_alerted_at).
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const ALERT_COOLDOWN_MS = 24 * 60 * 60 * 1000;

async function sendTelegram(token: string, chatId: string, text: string): Promise<boolean> {
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    });
    return r.ok;
  } catch (e) {
    console.warn("[toxic-alerter] telegram failed:", String((e as Error)?.message ?? e));
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
    const token = Deno.env.get("TELEGRAM_API_KEY_1");
    if (!token) {
      return new Response(JSON.stringify({ ok: false, error: "TELEGRAM_API_KEY_1 missing" }), {
        status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // 1) знайти токсичні правила
    const { data: rules, error } = await sb
      .from("ai_memory")
      .select("id, agent, pattern_key, learned_rule, success_count, failure_count, evidence, last_observed_at, is_active")
      .eq("is_active", false)
      .eq("category", "action-effectiveness")
      .gte("last_observed_at", new Date(Date.now() - 7 * 86400_000).toISOString())
      .limit(50);
    if (error) throw error;

    // 2) знайти chat_id адмінів
    const { data: admins } = await sb
      .from("user_roles")
      .select("user_id")
      .eq("role", "admin");
    const adminIds = (admins ?? []).map((a) => a.user_id as string);

    let adminChats: number[] = [];
    if (adminIds.length > 0) {
      // INC-0003 hotfix: customers has no user_id. Admin chat_id lives in telegram_chat_ids.
      const { data: rows } = await sb
        .from("telegram_chat_ids")
        .select("chat_id")
        .in("user_id", adminIds)
        .not("chat_id", "is", null);
      adminChats = ((rows ?? []) as Array<{ chat_id: number | null }>)
        .map((c) => (c.chat_id == null ? null : Number(c.chat_id)))
        .filter((x): x is number => x !== null);
    }

    // fallback: hardcoded admin chat from env
    if (adminChats.length === 0) {
      const fallback = Deno.env.get("TELEGRAM_ADMIN_CHAT_ID");
      if (fallback) adminChats = [Number(fallback)];
    }

    const now = Date.now();
    let alerted = 0;
    let skipped = 0;
    const details: Array<Record<string, unknown>> = [];

    for (const rule of rules ?? []) {
      const total = (rule.success_count as number) + (rule.failure_count as number);
      if (total < 5) { skipped++; continue; }
      const rate = total > 0 ? (rule.success_count as number) / total : 0;
      if (rate >= 0.3) { skipped++; continue; }

      // dedup
      const ev = (rule.evidence as Record<string, unknown>) ?? {};
      const lastAlertedAt = ev.last_alerted_at ? new Date(ev.last_alerted_at as string).getTime() : 0;
      if (now - lastAlertedAt < ALERT_COOLDOWN_MS) { skipped++; continue; }

      if (adminChats.length === 0) {
        details.push({ pattern_key: rule.pattern_key, action: "skip_no_admin_chat" });
        skipped++;
        continue;
      }

      const text =
        `⚠️ <b>Токсичний патерн виявлено</b>\n\n` +
        `<b>Агент:</b> <code>${rule.agent}</code>\n` +
        `<b>Pattern:</b> <code>${rule.pattern_key}</code>\n` +
        `<b>Успіх:</b> ${rule.success_count}/${total} (${Math.round(rate * 100)}%)\n\n` +
        `${rule.learned_rule}\n\n` +
        `Tribunal автоматично уникатиме цього патерну. Перевір у /admin/ai-memory.`;

      let sentTo = 0;
      for (const chat of adminChats) {
        const ok = await sendTelegram(token, String(chat), text);
        if (ok) sentTo++;
      }

      if (sentTo > 0) {
        await sb.from("ai_memory").update({
          evidence: { ...ev, last_alerted_at: new Date().toISOString() },
        }).eq("id", rule.id);
        alerted++;
      }

      details.push({ pattern_key: rule.pattern_key, sent_to: sentTo });
    }

    return new Response(
      JSON.stringify({ ok: true, scanned: rules?.length ?? 0, alerted, skipped, admin_chats: adminChats.length, details }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("toxic-pattern-alerter error", err);
    return new Response(
      JSON.stringify({ ok: false, error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
