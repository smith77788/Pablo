/**
 * 🩺 Outreach Self-Heal
 *
 * Запуск: cron щогодини.
 *
 * Що робить:
 *  1. Знаходить outreach_actions у статусі "failed" з retry_count < 3
 *     і failed_reason що НЕ містить permanent-патерн (rate_limit_exceeded,
 *     token_invalid, banned, deleted_by_moderator).
 *     → ставить scheduled_for = now()+15min, retry_count++, status="pending_review"
 *       (executor підхопить).
 *
 *  2. Якщо за останні 24h канал має >=5 failed AND <2 posted, авто-вимикає
 *     постинг для цього каналу через outreach_settings.{channel}_posting_enabled = false
 *     і пише debug_report про це. Канал можна руками увімкнути назад.
 *
 *  3. Логує self-run.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const PERMANENT_PATTERNS = [
  "rate_limit_exceeded", "token_invalid", "auth_failed", "banned",
  "deleted_by_moderator", "permanent_block", "spam_detected",
  "tribunal_reject", "tribunal_enqueue", "guard_reject",
];

const CHANNELS = ["reddit", "google", "telegram", "instagram"] as const;

interface FailedAction {
  id: string;
  channel: string;
  retry_count: number | null;
  failed_reason: string | null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const body = await req.clone().json().catch(() => ({}));

  return await withAgentRun("outreach-self-heal", detectTrigger(req, body), async () => {
    const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
    const stats = { rescheduled: 0, channels_paused: 0, examined: 0, skipped_permanent: 0 };
    const detail: any[] = [];

    // ── 1. Reschedule transient failures ────────────────────────────
    const since6h = new Date(Date.now() - 6 * 3600 * 1000).toISOString();
    const { data: failed } = await sb
      .from("outreach_actions")
      .select("id, channel, retry_count, failed_reason")
      .eq("status", "failed")
      .gte("created_at", since6h)
      .lt("retry_count", 3)
      .limit(100);

    // Group transient failures by new retry_count for batch updates (≤3 DB calls instead of N).
    const next = new Date(Date.now() + 15 * 60_000).toISOString();
    const rescheduleGroups = new Map<number, { ids: string[]; items: Array<{ id: string; channel: string; retry: number }> }>();
    for (const a of (failed ?? []) as FailedAction[]) {
      stats.examined++;
      const reason = (a.failed_reason ?? "").toLowerCase();
      const isPermanent = PERMANENT_PATTERNS.some((p) => reason.includes(p));
      if (isPermanent) { stats.skipped_permanent++; continue; }
      const newRetry = (a.retry_count ?? 0) + 1;
      if (!rescheduleGroups.has(newRetry)) rescheduleGroups.set(newRetry, { ids: [], items: [] });
      const grp = rescheduleGroups.get(newRetry)!;
      grp.ids.push(a.id);
      grp.items.push({ id: a.id, channel: a.channel, retry: newRetry });
    }
    await Promise.all(
      [...rescheduleGroups.entries()].map(async ([newRetry, { ids, items }]) => {
        const { error } = await sb.from("outreach_actions").update({
          status: "pending_review",
          scheduled_for: next,
          retry_count: newRetry,
        }).in("id", ids);
        if (!error) {
          stats.rescheduled += ids.length;
          detail.push(...items);
        }
      }),
    );

    // ── 2. Auto-pause unhealthy channels ────────────────────────────
    // Pre-fetch all channel settings and parallelize all 8 count queries at once.
    const since24h = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    const settingKeys = CHANNELS.map((ch) => `${ch}_posting_enabled`);
    const [{ data: settingsData }, channelCounts] = await Promise.all([
      sb.from("outreach_settings").select("key, value").in("key", settingKeys),
      Promise.all(
        CHANNELS.map((ch) => Promise.all([
          sb.from("outreach_actions").select("*", { count: "exact", head: true })
            .eq("channel", ch).eq("status", "failed").gte("created_at", since24h),
          sb.from("outreach_actions").select("*", { count: "exact", head: true })
            .eq("channel", ch).eq("status", "posted").gte("created_at", since24h),
        ])),
      ),
    ]);
    const settingsMap = new Map<string, boolean>(
      (settingsData ?? []).map((r: any) => [r.key as string, r.value as boolean]),
    );

    const channelsToPause: Array<{ ch: string; f: number; p: number }> = [];
    for (let i = 0; i < CHANNELS.length; i++) {
      const ch = CHANNELS[i];
      const [{ count: failedCnt }, { count: postedCnt }] = channelCounts[i];
      const f = failedCnt ?? 0;
      const p = postedCnt ?? 0;
      if (f >= 5 && p < 2) {
        const currentlyEnabled = settingsMap.get(`${ch}_posting_enabled`) ?? false;
        if (currentlyEnabled) channelsToPause.push({ ch, f, p });
      }
    }

    if (channelsToPause.length > 0) {
      await Promise.all([
        sb.from("outreach_settings").upsert(
          channelsToPause.map(({ ch, f, p }) => ({
            key: `${ch}_posting_enabled`,
            value: false,
            description: `Auto-paused by self-heal (${f} failed / ${p} posted in 24h)`,
          })),
          { onConflict: "key" },
        ),
        ...channelsToPause.map(({ ch, f, p }) =>
          (sb as any).rpc("ingest_debug_report", {
            p_platform: "edge_function",
            p_level: "warn",
            p_source: "outreach-self-heal",
            p_message: `Channel ${ch} auto-paused: ${f} failed actions vs ${p} posted in 24h`,
            p_stack: null,
            p_context: { channel: ch, failed_24h: f, posted_24h: p },
            p_fingerprint: `outreach-channel-paused:${ch}`,
          } as any).catch(() => {}),
        ),
      ]);
      stats.channels_paused = channelsToPause.length;
      for (const { ch, f, p } of channelsToPause) {
        detail.push({ channel_paused: ch, failed: f, posted: p });
      }
    }

    return {
      result: new Response(JSON.stringify({ ok: true, stats, detail }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }),
      summary: `rescheduled=${stats.rescheduled}, paused=${stats.channels_paused}, skipped_perm=${stats.skipped_permanent}`,
      payload: { stats },
      status: stats.channels_paused > 0 ? "partial" : "success",
    };
  }).catch((e) => {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  });
});
