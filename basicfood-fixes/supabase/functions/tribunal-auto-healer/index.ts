/**
 * 🩹 Tribunal Auto-Healer
 *
 * Запуск: cron кожні 30 хв.
 *
 * Що робить:
 *  1. Бере відкриті `debug_reports` з source IN
 *     ('agent-health-watcher','outreach-self-heal') рівнем warn/error/fatal,
 *     які НЕ мають resolved_at.
 *  2. Для кожного фінгерпринта виконує безпечний healing:
 *     - `agent-silence:{name}` → викликає функцію {name} (manual-trigger),
 *       якщо викликається успішно — позначає звіт як resolved.
 *     - `agent-error-burst:{name}` → НЕ авто-перезапускає, а позначає як
 *       acknowledged (status='investigating') щоб людина зайнялась.
 *     - `agent-long-run:{name}` → також 'investigating'.
 *     - `outreach-channel-paused:{ch}` → залишає, ця подія керується
 *       вручну з UI (Resume button).
 *  3. Усе логується через withAgentRun.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { detectTrigger, withAgentRun } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

// Whitelist агентів, яких безпечно перезапускати при silence.
// (НЕ включаємо ті, що пишуть у БД деструктивно: pricing engine, executor — для них
// краще щоб людина проглянула.)
const SAFE_TO_RESTART = new Set<string>([
  "outreach-composer",
  "outreach-reddit-hunter",
  "outreach-google-hunter",
  "outreach-telegram-hunter",
  "outreach-instagram-hunter",
  "outreach-roi-collector",
  "outreach-quality-scorer",
  "tribunal-orchestrator",
  "agent-health-watcher",
  "outreach-self-heal",
]);

const OPEN_DEBUG_STATUSES = ["new", "triaged", "auto_fixing", "manual_required"] as const;
const MANUAL_REVIEW_STATUS = "manual_required";

async function callEdge(name: string): Promise<{ ok: boolean; status?: number; err?: string }> {
  try {
    const res = await fetch(`${SUPABASE_URL}/functions/v1/${name}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
        apikey: SUPABASE_SERVICE_ROLE_KEY,
      },
      body: JSON.stringify({ trigger: "auto_healer" }),
    });
    return { ok: res.ok, status: res.status };
  } catch (e) {
    return { ok: false, err: String((e as Error)?.message ?? e) };
  }
}

interface DebugReportRow {
  id: string;
  fingerprint: string;
  source: string | null;
  level: string;
  message: string;
  status: string;
  occurrences: number;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const body = await req.clone().json().catch(() => ({}));

  return await withAgentRun("tribunal-auto-healer", detectTrigger(req, body), async () => {
    const sb = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
    const stats = { restarted: 0, ack_burst: 0, ack_long: 0, skipped: 0, examined: 0 };
    const detail: any[] = [];

    const { data: reports } = await sb
      .from("debug_reports")
      .select("id, fingerprint, source, level, message, status, occurrences")
      .in("source", ["agent-health-watcher", "outreach-self-heal"])
      .in("status", [...OPEN_DEBUG_STATUSES])
      .is("resolved_at", null)
      .order("last_seen_at", { ascending: false })
      .limit(50);

    // Categorize reports without any DB calls in the loop.
    const silenceTargets: Array<{ id: string; fp: string; target: string }> = [];
    const ackIds: string[] = [];

    for (const r of (reports ?? []) as DebugReportRow[]) {
      stats.examined++;
      const fp = r.fingerprint ?? "";

      const silenceMatch = fp.match(/^agent-silence:(.+)$/);
      if (silenceMatch) {
        const target = silenceMatch[1];
        if (!SAFE_TO_RESTART.has(target)) {
          stats.skipped++;
          detail.push({ fp, action: "skip_unsafe", target });
        } else {
          silenceTargets.push({ id: r.id, fp, target });
        }
        continue;
      }

      if (fp.startsWith("agent-error-burst:")) {
        ackIds.push(r.id);
        stats.ack_burst++;
        detail.push({ fp, action: "ack_burst" });
        continue;
      }

      if (fp.startsWith("agent-long-run:")) {
        ackIds.push(r.id);
        stats.ack_long++;
        detail.push({ fp, action: "ack_long" });
        continue;
      }

      if (fp.startsWith("outreach-channel-paused:")) {
        stats.skipped++;
        detail.push({ fp, action: "manual_only" });
        continue;
      }

      stats.skipped++;
    }

    // Parallelize silence restarts; batch ack for error-burst + long-run.
    const silenceResults = await Promise.all(
      silenceTargets.map(async ({ id, fp, target }) => {
        const out = await callEdge(target);
        return { id, fp, target, out };
      }),
    );

    const writeBatch: Promise<unknown>[] = [];

    if (ackIds.length > 0) {
      writeBatch.push(
        sb.from("debug_reports").update({
          status: MANUAL_REVIEW_STATUS,
          auto_fix_action: "needs_human_review",
        }).in("id", ackIds),
      );
    }

    const resolvedAt = new Date().toISOString();
    for (const { id, fp, target, out } of silenceResults) {
      if (out.ok) {
        writeBatch.push(
          sb.from("debug_reports").update({
            status: "fixed",
            resolved_at: resolvedAt,
            auto_fix_action: `restart:${target}`,
          }).eq("id", id),
        );
        stats.restarted++;
        detail.push({ fp, action: "restart_ok", target });
      } else {
        writeBatch.push(
          sb.from("debug_reports").update({
            status: MANUAL_REVIEW_STATUS,
            auto_fix_action: `restart_failed:${target}:${out.status ?? out.err ?? "?"}`,
          }).eq("id", id),
        );
        detail.push({ fp, action: "restart_failed", target, status: out.status, err: out.err });
      }
    }

    if (writeBatch.length > 0) await Promise.all(writeBatch);

    return {
      result: new Response(JSON.stringify({ ok: true, stats, detail }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }),
      summary: `restarted=${stats.restarted}, ack_burst=${stats.ack_burst}, ack_long=${stats.ack_long}, skipped=${stats.skipped}`,
      payload: { stats, examined: stats.examined },
      status: stats.restarted > 0 || stats.ack_burst > 0 || stats.ack_long > 0 ? "partial" : "success",
    };
  }).catch((e) => {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  });
});
