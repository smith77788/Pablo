// CheckoutFailureWatcher — hourly sentinel.
// Scans the events table for checkout_failed in the last hour, groups by
// failure reason, and files one debug_report per unique reason so the
// operator team is paged. Prevents silent revenue loss like INC-001
// (RLS race condition that cost a 176 UAH cart).
//
// NOTE: Uses the shared `ingest_debug_report` RPC which handles validation,
// dedup-by-fingerprint, and occurrence bumping atomically. Manual inserts
// into debug_reports are forbidden — they bypass enum validation and silently
// fail under RLS (caused this watcher to be a no-op before 2026-04-29).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const since = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const { data: failures, error } = await supabase
    .from("events")
    .select("metadata, created_at")
    .eq("event_type", "checkout_failed")
    .gte("created_at", since);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  // Group by reason
  const byReason = new Map<string, { count: number; total_value: number; last_at: string }>();
  for (const f of failures ?? []) {
    const reason = String((f.metadata as any)?.reason ?? "unknown").slice(0, 200);
    const value = Number((f.metadata as any)?.cart_value ?? 0);
    const cur = byReason.get(reason) ?? { count: 0, total_value: 0, last_at: f.created_at };
    cur.count += 1;
    cur.total_value += value;
    if (f.created_at > cur.last_at) cur.last_at = f.created_at;
    byReason.set(reason, cur);
  }

  let reportsFiled = 0;
  const filed: any[] = [];

  for (const [reason, stats] of byReason) {
    // Stable fingerprint — RPC dedups & bumps occurrences when already open.
    // Must be 8-128 chars per ingest_debug_report validation.
    const rawFp = `checkout_failed::${reason}`;
    const fp = rawFp.length < 8 ? rawFp.padEnd(8, "_") : rawFp.slice(0, 128);

    const { error: rpcErr } = await (supabase as any).rpc("ingest_debug_report", {
      p_platform: "web",
      p_level: "error",
      p_source: "CheckoutFailureWatcher",
      p_message: `Checkout failed (${stats.count}x): ${reason}`,
      p_stack: null,
      p_context: {
        reason,
        occurrence_count: stats.count,
        total_lost_value_uah: stats.total_value,
        last_seen_at: stats.last_at,
      },
      p_fingerprint: fp,
    });

    if (!rpcErr) {
      reportsFiled++;
      filed.push({ reason, count: stats.count, lost_uah: stats.total_value });
    } else {
      console.error("[checkout-failure-watcher] ingest_debug_report failed:", rpcErr.message);
    }
  }

  return new Response(
    JSON.stringify({
      scanned: failures?.length ?? 0,
      unique_reasons: byReason.size,
      reports_filed: reportsFiled,
      filed,
    }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
