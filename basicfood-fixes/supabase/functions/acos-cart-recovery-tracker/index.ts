// ACOS Cart Recovery Tracker
//
// Marks cart_recovery_attempts as recovered when the matching customer/chat
// has a successful order within 48h after the attempt was sent. Also expires
// stale attempts (no purchase in 48h → status='expired').

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { beginQuickAgentRun } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  const __agent = beginQuickAgentRun("acos-cart-recovery-tracker", req);

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const cutoff = new Date(Date.now() - 48 * 3600_000).toISOString();

    const { data: attempts } = await supabase
      .from("cart_recovery_attempts")
      .select("id, chat_id, customer_id, session_id, created_at, cart_value")
      .eq("status", "sent")
      .gte("created_at", cutoff);

    const allAttempts = attempts ?? [];

    // Batch-prefetch customers by chat_id and session purchase events
    const chatIds = [...new Set(allAttempts.map((a) => a.chat_id).filter(Boolean))];
    const sessionIds = [...new Set(allAttempts.map((a) => a.session_id).filter(Boolean))];

    const [custRows, sessionEventRows] = await Promise.all([
      chatIds.length
        ? supabase.from("customers").select("id, telegram_chat_id").in("telegram_chat_id", chatIds)
        : Promise.resolve({ data: [] as any[] }),
      sessionIds.length
        ? supabase.from("events").select("order_id, session_id, created_at")
          .eq("event_type", "purchase_completed").in("session_id", sessionIds)
        : Promise.resolve({ data: [] as any[] }),
    ]);

    const custByChatId = new Map<number, string>(
      (custRows.data ?? []).filter((c: any) => c.telegram_chat_id).map((c: any) => [Number(c.telegram_chat_id), c.id as string])
    );

    // Collect event order_ids for batch-prefetch
    const eventOrderIds = [...new Set((sessionEventRows.data ?? []).map((e: any) => e.order_id).filter(Boolean))];
    const { data: eventOrderRows } = eventOrderIds.length
      ? await supabase.from("orders").select("id, total, created_at").in("id", eventOrderIds)
      : { data: [] as any[] };
    const orderByEventId = new Map<string, any>((eventOrderRows ?? []).map((o: any) => [o.id, o]));

    // Map session_id → first purchase event (sorted by created_at)
    const purchaseBySession = new Map<string, { order_id: string; created_at: string }>();
    for (const e of (sessionEventRows.data ?? []) as any[]) {
      if (!e.session_id || !e.order_id) continue;
      const existing = purchaseBySession.get(e.session_id);
      if (!existing || e.created_at < existing.created_at) purchaseBySession.set(e.session_id, e);
    }

    // Process attempts in parallel; per-chat-id order lookups remain per-attempt
    // (different time windows prevent batch-fetch), but customer reads are eliminated.
    const recoveredIds: string[] = [];
    const recoveredUpdates: any[] = [];
    const expiredIds: string[] = [];

    await Promise.all(allAttempts.map(async (a) => {
      let matchedOrder: { id: string; total: number; created_at: string } | null = null;

      if (a.chat_id && custByChatId.has(Number(a.chat_id))) {
        const { data: ord } = await supabase
          .from("orders")
          .select("id, total, created_at, customer_phone, customer_email")
          .gte("created_at", a.created_at)
          .lte("created_at", new Date(new Date(a.created_at).getTime() + 48 * 3600_000).toISOString())
          .neq("status", "cancelled")
          .order("created_at", { ascending: true })
          .limit(20);
        if (ord && ord.length) matchedOrder = (ord[0] as any) ?? null;
      }

      if (!matchedOrder && a.session_id) {
        const ev = purchaseBySession.get(a.session_id);
        if (ev?.order_id && ev.created_at >= a.created_at) {
          const ord = orderByEventId.get(ev.order_id);
          if (ord) matchedOrder = ord as any;
        }
      }

      if (matchedOrder) {
        recoveredUpdates.push({ id: a.id, order: matchedOrder });
      } else if (new Date(a.created_at).getTime() < Date.now() - 48 * 3600_000) {
        expiredIds.push(a.id);
      }
    }));

    const recoveredAt = new Date().toISOString();
    const [recResults, expResults] = await Promise.all([
      Promise.all(recoveredUpdates.map(({ id, order }) =>
        supabase.from("cart_recovery_attempts").update({
          status: "recovered",
          recovered_at: recoveredAt,
          recovered_order_id: order.id,
          recovered_value: order.total,
        }).eq("id", id)
      )),
      expiredIds.length
        ? supabase.from("cart_recovery_attempts").update({ status: "expired" }).in("id", expiredIds)
        : Promise.resolve(null),
    ]);
    const recovered = recoveredUpdates.length;
    const expired = expiredIds.length;

    __agent.success();
    return new Response(JSON.stringify({ ok: true, scanned: attempts?.length ?? 0, recovered, expired }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    __agent.error(e);
    console.error("[cart-recovery-tracker] fatal", e);
    return new Response(JSON.stringify({ error: String((e as Error)?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
