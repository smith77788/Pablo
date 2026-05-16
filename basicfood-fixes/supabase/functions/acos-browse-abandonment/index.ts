// Cycle #15 — Browse Abandonment Engine
// Detects visitors that viewed products in last 24h without purchasing
// and creates browse_abandonment_signals + an aggregated insight.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-browse-abandonment", req);

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const sinceISO = new Date(Date.now() - 24 * 3600_000).toISOString();

    // Pull product_view events from last 24h
    const { data: views } = await supabase
      .from("events")
      .select("session_id, user_id, product_id, created_at, metadata")
      .eq("event_type", "product_view")
      .gte("created_at", sinceISO)
      .limit(10_000);

    // Pull purchases in same window (visitors that already bought)
    const { data: purchases } = await supabase
      .from("events")
      .select("session_id, user_id")
      .eq("event_type", "purchase")
      .gte("created_at", sinceISO)
      .limit(5_000);

    const purchasedSessions = new Set<string>();
    const purchasedUsers = new Set<string>();
    for (const p of purchases ?? []) {
      if (p.session_id) purchasedSessions.add(p.session_id);
      if (p.user_id) purchasedUsers.add(p.user_id);
    }

    // Group views by visitor (session_id as fallback identifier)
    type Bucket = {
      visitor_id: string;
      user_id: string | null;
      products: Set<string>;
      lastSeen: string;
      count: number;
    };
    const buckets = new Map<string, Bucket>();

    for (const v of (views ?? []) as any[]) {
      const visitorId = v.session_id || v.user_id;
      if (!visitorId) continue;
      if (purchasedSessions.has(visitorId) || (v.user_id && purchasedUsers.has(v.user_id))) continue;
      if (!v.product_id) continue;

      const b = buckets.get(visitorId) ?? {
        visitor_id: visitorId,
        user_id: v.user_id ?? null,
        products: new Set<string>(),
        lastSeen: v.created_at,
        count: 0,
      };
      b.products.add(v.product_id);
      b.count++;
      if (v.created_at > b.lastSeen) b.lastSeen = v.created_at;
      buckets.set(visitorId, b);
    }

    // Only signals with ≥2 product views or ≥3 total views
    const rows: any[] = [];
    for (const b of buckets.values()) {
      if (b.products.size < 2 && b.count < 3) continue;
      rows.push({
        visitor_id: b.visitor_id,
        user_id: b.user_id,
        product_ids: Array.from(b.products),
        view_count: b.count,
        last_viewed_at: b.lastSeen,
        recovery_status: "pending",
      });
    }

    // Upsert by visitor_id (delete pending older than 7d, insert fresh)
    if (rows.length) {
      // Drop stale pending rows for the same visitors
      const visitorIds = rows.map((r) => r.visitor_id);
      await supabase
        .from("browse_abandonment_signals")
        .delete()
        .in("visitor_id", visitorIds)
        .eq("recovery_status", "pending");
      // Insert in chunks
      const chunk = 500;
      const chunks: any[][] = [];
      for (let i = 0; i < rows.length; i += chunk) chunks.push(rows.slice(i, i + chunk));
      await Promise.all(chunks.map((c) => supabase.from("browse_abandonment_signals").insert(c)));
    }

    if (rows.length >= 5) {
      const totalProducts = rows.reduce((s, r) => s + r.product_ids.length, 0);
      await supabase.from("ai_insights").insert({
        insight_type: "browse_abandonment",
        title: `${rows.length} відвідувачів покинули перегляд без покупки`,
        description: `За 24 години ${rows.length} візитерів переглянули ${totalProducts} карток без покупки. Запустіть recovery touch (push/email/Telegram) для топ-сегмента.`,
        confidence: 0.7,
        risk_level: "medium",
        affected_layer: "marketing",
        metrics: { abandoned_visitors: rows.length, total_products_viewed: totalProducts },
      });
    }

    __agent.success();
    return new Response(JSON.stringify({ ok: true, abandoned: rows.length }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    __agent.error(e);
    console.error("browse-abandonment error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
