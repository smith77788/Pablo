// ACOS Cycle #10 — Multi-Touch Attribution
// Reconstructs customer journeys from events table and distributes conversion credit
// across all touchpoints using three attribution models:
//   - first_touch  : 100% to first session source
//   - last_touch   : 100% to last source before purchase
//   - linear       : equal split across all unique sources in journey
//   - position     : 40% first / 40% last / 20% middle (industry standard)
//
// Identifies:
//   - over-credited channels (last-click bias) — channels that look good only because they're last
//   - under-credited channels (assist channels) — top-of-funnel drivers ignored by simple models
//   - "lonely converters" — sessions with single touchpoint (true direct response)
//
// Output: ai_insights of type='attribution_finding' with channel re-allocation recommendations.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const DAY_MS = 24 * 60 * 60 * 1000;
const LOOKBACK_DAYS = 30;
const JOURNEY_WINDOW_DAYS = 14; // how far back to look for touchpoints before purchase

interface EventRow {
  session_id: string | null;
  user_id: string | null;
  event_type: string;
  metadata: Record<string, unknown>;
  url: string | null;
  user_agent: string | null;
  created_at: string;
  order_id: string | null;
}

interface OrderRow {
  id: string;
  user_id: string | null;
  customer_phone: string | null;
  customer_email: string | null;
  total: number;
  created_at: string;
}

const channelFromEvent = (e: EventRow): string => {
  const meta = e.metadata ?? {};
  const ref = typeof meta.referrer === "string" ? meta.referrer.toLowerCase() : "";
  const utmSource = typeof (meta as any).utm_source === "string" ? (meta as any).utm_source.toLowerCase() : "";
  const utmMedium = typeof (meta as any).utm_medium === "string" ? (meta as any).utm_medium.toLowerCase() : "";

  if (utmSource) {
    if (utmSource.includes("instagram") || utmSource.includes("ig")) return "instagram";
    if (utmSource.includes("tiktok")) return "tiktok";
    if (utmSource.includes("telegram") || utmSource.includes("tg")) return "telegram";
    if (utmSource.includes("google")) return utmMedium === "cpc" ? "google_ads" : "google_organic";
    if (utmSource.includes("facebook") || utmSource.includes("meta")) return "meta_ads";
    return `utm:${utmSource}`;
  }

  if (!ref) return "direct";
  if (ref.includes("instagram")) return "instagram";
  if (ref.includes("tiktok")) return "tiktok";
  if (ref.includes("t.me") || ref.includes("telegram")) return "telegram";
  if (ref.includes("google")) return "google_organic";
  if (ref.includes("facebook") || ref.includes("fb.")) return "meta_organic";
  if (ref.includes("youtube")) return "youtube";
  return "referral_other";
};

const customerKey = (o: OrderRow): string =>
  o.user_id || o.customer_phone || o.customer_email || `unknown:${o.id}`;

interface ChannelCredit {
  first_touch: number;
  last_touch: number;
  linear: number;
  position: number;
  conversions: number;
  assist_count: number;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-multi-touch-attribution", req, null, async () => {
    const __res = await (async () => {

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    const now = Date.now();
    const since = new Date(now - LOOKBACK_DAYS * DAY_MS).toISOString();
    const journeyStart = new Date(now - (LOOKBACK_DAYS + JOURNEY_WINDOW_DAYS) * DAY_MS).toISOString();

    // 1. Pull recent purchases
    const { data: orders, error: oErr } = await supabase
      .from("orders")
      .select("id, user_id, customer_phone, customer_email, total, created_at")
      .gte("created_at", since)
      .neq("status", "cancelled")
      .limit(2000);
    if (oErr) throw oErr;

    if (!orders || orders.length === 0) {
      return new Response(
        JSON.stringify({ ok: true, message: "No orders in window", checked: 0 }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // 2. Pull events in journey window — page_viewed + product_viewed for source attribution
    const { data: events, error: eErr } = await supabase
      .from("events")
      .select("session_id, user_id, event_type, metadata, url, user_agent, created_at, order_id")
      .in("event_type", ["page_viewed", "product_viewed", "purchase_completed"])
      .gte("created_at", journeyStart)
      .limit(50000);
    if (eErr) throw eErr;

    // 3. Index events by user_id and session_id
    const eventsByUser = new Map<string, EventRow[]>();
    const eventsBySession = new Map<string, EventRow[]>();
    for (const ev of (events ?? []) as EventRow[]) {
      if (ev.user_id) {
        const arr = eventsByUser.get(ev.user_id) ?? [];
        arr.push(ev);
        eventsByUser.set(ev.user_id, arr);
      }
      if (ev.session_id) {
        const arr = eventsBySession.get(ev.session_id) ?? [];
        arr.push(ev);
        eventsBySession.set(ev.session_id, arr);
      }
    }

    // 4. For each order, reconstruct journey
    const channelCredit: Map<string, ChannelCredit> = new Map();
    const totalConversions = orders.length;
    let lonelyConverters = 0;
    let multiTouchConverters = 0;

    const ensure = (ch: string): ChannelCredit => {
      let c = channelCredit.get(ch);
      if (!c) {
        c = { first_touch: 0, last_touch: 0, linear: 0, position: 0, conversions: 0, assist_count: 0 };
        channelCredit.set(ch, c);
      }
      return c;
    };

    for (const order of orders as OrderRow[]) {
      // Find related events: user_id match preferred, fallback to events with this order_id
      const orderTime = new Date(order.created_at).getTime();
      const windowStart = orderTime - JOURNEY_WINDOW_DAYS * DAY_MS;

      let candidateEvents: EventRow[] = [];
      if (order.user_id && eventsByUser.has(order.user_id)) {
        candidateEvents = eventsByUser.get(order.user_id)!;
      }
      // Augment with events tied directly via order_id
      const direct = (events ?? []).filter((e: EventRow) => e.order_id === order.id);
      candidateEvents = [...candidateEvents, ...direct];

      // Filter to journey window before purchase
      const journey = candidateEvents
        .filter((e) => {
          const t = new Date(e.created_at).getTime();
          return t >= windowStart && t <= orderTime + 60_000; // +1min buffer
        })
        .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());

      if (journey.length === 0) {
        // No traceable journey → attribute to "direct"
        const c = ensure("direct");
        c.first_touch += order.total;
        c.last_touch += order.total;
        c.linear += order.total;
        c.position += order.total;
        c.conversions += 1;
        lonelyConverters += 1;
        continue;
      }

      // Extract unique channel sequence (preserve order, dedupe consecutive)
      const channelSeq: string[] = [];
      for (const ev of journey) {
        const ch = channelFromEvent(ev);
        if (channelSeq[channelSeq.length - 1] !== ch) channelSeq.push(ch);
      }

      const uniqueChannels = Array.from(new Set(channelSeq));
      const orderValue = order.total || 0;

      if (uniqueChannels.length === 1) {
        lonelyConverters += 1;
        const c = ensure(uniqueChannels[0]);
        c.first_touch += orderValue;
        c.last_touch += orderValue;
        c.linear += orderValue;
        c.position += orderValue;
        c.conversions += 1;
        continue;
      }

      multiTouchConverters += 1;

      // First touch
      ensure(uniqueChannels[0]).first_touch += orderValue;
      // Last touch
      ensure(uniqueChannels[uniqueChannels.length - 1]).last_touch += orderValue;
      // Linear
      const linearShare = orderValue / uniqueChannels.length;
      for (const ch of uniqueChannels) {
        ensure(ch).linear += linearShare;
      }
      // Position-based (40/40/20)
      const first = uniqueChannels[0];
      const last = uniqueChannels[uniqueChannels.length - 1];
      const middle = uniqueChannels.slice(1, -1);
      ensure(first).position += orderValue * 0.4;
      ensure(last).position += orderValue * 0.4;
      if (middle.length > 0) {
        const midShare = (orderValue * 0.2) / middle.length;
        for (const ch of middle) ensure(ch).position += midShare;
      } else {
        // Only 2 channels — split the 20% equally
        ensure(first).position += orderValue * 0.1;
        ensure(last).position += orderValue * 0.1;
      }

      // Track conversions + assists
      for (const ch of uniqueChannels) {
        const c = ensure(ch);
        if (ch === last) c.conversions += 1;
        else c.assist_count += 1;
      }
    }

    // 5. Build channel report + detect over/under-credited
    const channelReport = Array.from(channelCredit.entries())
      .map(([channel, c]) => {
        const lastVsPosition = c.last_touch - c.position;
        const lastVsFirst = c.last_touch - c.first_touch;
        return {
          channel,
          first_touch: c.first_touch,
          last_touch: c.last_touch,
          linear: c.linear,
          position: c.position,
          conversions: c.conversions,
          assist_count: c.assist_count,
          // Positive = over-credited by last-touch (looks better than reality)
          last_touch_bias_uah: lastVsPosition,
          // Negative = assist channel (drives traffic, doesn't close)
          first_vs_last_uah: lastVsFirst,
        };
      })
      .sort((a, b) => b.position - a.position);

    // 6. Generate insights for over-credited and under-credited channels (batch)
    const totalRevenue = channelReport.reduce((s, c) => s + c.position, 0) || 1;
    const insightRows: any[] = [];

    for (const ch of channelReport) {
      if (ch.position < totalRevenue * 0.02) continue; // ignore noise channels (<2%)

      const biasPct = Math.abs(ch.last_touch_bias_uah) / Math.max(ch.position, 1);

      // Over-credited: last-touch shows ≥30% more revenue than position model
      if (ch.last_touch_bias_uah > 0 && biasPct >= 0.3 && ch.last_touch > 500) {
        insightRows.push({
          insight_type: "attribution_finding",
          affected_layer: "growth",
          risk_level: "low",
          title: `Канал «${ch.channel}» переоцінений last-click моделлю`,
          description: `Last-touch приписує каналу ${ch.last_touch.toFixed(0)} ₴, але position-based показує лише ${ch.position.toFixed(0)} ₴ (різниця ${ch.last_touch_bias_uah.toFixed(0)} ₴). Канал часто закриває угоду, але не запускає її — не масштабуй бюджет на основі last-click ROI.`,
          confidence: 0.8,
          expected_impact: `Перерозподіл бюджету може звільнити ~${Math.round(ch.last_touch_bias_uah * 0.3)} ₴/міс`,
          metrics: {
            channel: ch.channel,
            first_touch_uah: ch.first_touch,
            last_touch_uah: ch.last_touch,
            linear_uah: ch.linear,
            position_uah: ch.position,
            bias_uah: ch.last_touch_bias_uah,
            bias_pct: biasPct,
            conversions: ch.conversions,
            assists: ch.assist_count,
            target_entity: "channel",
            target_id: ch.channel,
          },
          status: "new",
        });
      }

      // Under-credited assist channel: assists ≥ 2× conversions AND first_touch > last_touch significantly
      if (ch.assist_count >= ch.conversions * 2 && ch.first_touch > ch.last_touch * 1.5 && ch.first_touch > 500) {
        insightRows.push({
          insight_type: "attribution_finding",
          affected_layer: "growth",
          risk_level: "low",
          title: `Канал «${ch.channel}» — недооцінений assist-драйвер`,
          description: `${ch.assist_count} асистів проти ${ch.conversions} прямих конверсій. First-touch revenue ${ch.first_touch.toFixed(0)} ₴ значно перевищує last-touch ${ch.last_touch.toFixed(0)} ₴. Цей канал відкриває воронку — обрізати його = втратити downstream конверсії.`,
          confidence: 0.8,
          expected_impact: `Збереження каналу = захист ~${Math.round(ch.first_touch * 0.4)} ₴/міс асистованої виручки`,
          metrics: {
            channel: ch.channel,
            first_touch_uah: ch.first_touch,
            last_touch_uah: ch.last_touch,
            linear_uah: ch.linear,
            position_uah: ch.position,
            conversions: ch.conversions,
            assists: ch.assist_count,
            target_entity: "channel",
            target_id: ch.channel,
          },
          status: "new",
        });
      }
    }

    let insightsInserted = 0;
    if (insightRows.length > 0) {
      await supabase.from("ai_insights").insert(insightRows).catch(() => {});
      insightsInserted = insightRows.length;
    }

    return new Response(
      JSON.stringify({
        ok: true,
        orders_analyzed: totalConversions,
        events_processed: events?.length ?? 0,
        lonely_converters: lonelyConverters,
        multi_touch_converters: multiTouchConverters,
        channels: channelReport,
        insights_inserted: insightsInserted,
        generated_at: new Date().toISOString(),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("acos-multi-touch-attribution error", err);
    return new Response(
      JSON.stringify({ ok: false, error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
    })();
    return { response: __res };
  });
});
