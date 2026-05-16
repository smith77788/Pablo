// ACOS Iter-14 — Insight Deduper (Stability agent)
// Cron: every 6h. Scans ai_insights for duplicates within the same hour bucket
// + insight_type and auto-archives the older ones, keeping only the most
// recent (highest confidence wins ties). Prevents Orchestrator/AdminInsights
// from being flooded with repetitive low-signal noise.
//
// Safety:
//   - never touches insights with status `auto_applied` or `accepted` (they
//     represent committed decisions — we don't want to lose audit trail)
//   - only operates on `new` and `dismissed` insights
//   - dry_run flag returns counts without mutating
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

interface Insight {
  id: string;
  insight_type: string;
  dedup_bucket: number | null;
  confidence: number;
  status: string;
  created_at: string;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  let dryRun = false;
  try {
    if (req.method === "POST") {
      const body = await req.json().catch(() => ({}));
      dryRun = !!body?.dry_run;
    }
  } catch { /* ignore */ }

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  // Pull only mutable insights from the last 30 days — older noise is
  // already cold and not worth re-scanning.
  const since = new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString();
  const { data: rows, error } = await supabase
    .from("ai_insights")
    .select("id, insight_type, dedup_bucket, confidence, status, created_at")
    .in("status", ["new", "dismissed"])
    .gte("created_at", since)
    .order("created_at", { ascending: false });

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Group by (insight_type, dedup_bucket). Null buckets aren't deduped — they
  // pre-date the trigger and we treat each as unique.
  const groups = new Map<string, Insight[]>();
  for (const r of (rows ?? []) as Insight[]) {
    if (r.dedup_bucket == null) continue;
    const key = `${r.insight_type}|${r.dedup_bucket}`;
    const arr = groups.get(key) ?? [];
    arr.push(r);
    groups.set(key, arr);
  }

  const toArchive: string[] = [];
  let groupsWithDupes = 0;

  for (const [, arr] of groups) {
    if (arr.length < 2) continue;
    groupsWithDupes++;
    // Keep the most recent; on tie keep highest confidence
    arr.sort((a, b) => {
      const t = new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      return t !== 0 ? t : b.confidence - a.confidence;
    });
    for (let i = 1; i < arr.length; i++) toArchive.push(arr[i].id);
  }

  if (toArchive.length === 0 || dryRun) {
    return new Response(
      JSON.stringify({
        ok: true,
        dry_run: dryRun,
        scanned: rows?.length ?? 0,
        groups_with_dupes: groupsWithDupes,
        would_archive: toArchive.length,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  // Update in parallel chunks of 200 to stay well under PostgREST limits.
  let archived = 0;
  if (toArchive.length > 0) {
    const archiveChunks: string[][] = [];
    for (let i = 0; i < toArchive.length; i += 200) archiveChunks.push(toArchive.slice(i, i + 200));
    const archiveResults = await Promise.all(
      archiveChunks.map((chunk) =>
        supabase.from("ai_insights").update({ status: "archived_duplicate", updated_at: new Date().toISOString() }).in("id", chunk)
      ),
    );
    for (const { error: updErr } of archiveResults) {
      if (updErr) {
        return new Response(JSON.stringify({ error: updErr.message, archived }), {
          status: 500,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
    }
    archived = toArchive.length;
  }

  return new Response(
    JSON.stringify({ ok: true, scanned: rows?.length ?? 0, groups_with_dupes: groupsWithDupes, archived }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
