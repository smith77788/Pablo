// Cycle #18 — UGC Review Harvester
// Sends review requests via Telegram for orders delivered ≥3 days ago without
// a review yet, and curates top published reviews into ugc_features.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

async function tgSend(chatId: number, text: string) {
  const token = Deno.env.get("TELEGRAM_API_KEY");
  if (!token) return false;
  const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" }),
  });
  return res.ok;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    // ----- 1) Send review requests -----
    const cutoff = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const fortnight = new Date(Date.now() - 14 * 86_400_000).toISOString();

    const { data: orders } = await supabase
      .from("orders")
      .select("id, customer_name, customer_phone, customer_email, total, updated_at, status")
      .eq("status", "delivered")
      .gte("updated_at", fortnight)
      .lte("updated_at", cutoff)
      .limit(100);

    let requested = 0;
    const orderList = orders ?? [];
    if (orderList.length > 0) {
      const orderIds = orderList.map((o: any) => o.id as string);
      const phones = [...new Set(
        orderList.filter((o: any) => o.customer_phone).map((o: any) => o.customer_phone as string),
      )];

      // Batch: which orders already have review requests + customer lookup by phone
      const [existingReqsRes, custRes] = await Promise.all([
        supabase.from("review_requests").select("order_id").in("order_id", orderIds),
        phones.length > 0
          ? supabase.from("customers").select("id, phone, telegram_chat_id").in("phone", phones)
          : Promise.resolve({ data: [] as any[] }),
      ]);
      const alreadyRequested = new Set(
        (existingReqsRes.data ?? []).map((r: any) => r.order_id as string),
      );
      const custByPhone = new Map(
        (custRes.data ?? []).map((c: any) => [c.phone as string, c]),
      );

      const newOrders = orderList.filter((o: any) => !alreadyRequested.has(o.id as string));

      // Parallel sends
      const sendResults = await Promise.all(
        newOrders.map(async (o: any) => {
          const cust = o.customer_phone ? custByPhone.get(o.customer_phone as string) : null;
          const chatId: number | null = cust?.telegram_chat_id ?? null;
          let channel = "pending";
          if (chatId) {
            const reviewUrl = "https://basic-food.shop/reviews";
            const ok = await tgSend(
              chatId,
              `Привіт, ${o.customer_name}! 🐾\nДякуємо за замовлення №${(o.id as string).slice(0, 8)}.\nБудемо вдячні за короткий відгук — це допомагає іншим власникам обирати: ${reviewUrl}`,
            );
            if (ok) channel = "telegram";
          }
          return {
            order_id: o.id as string,
            customer_id: cust?.id ?? null,
            channel,
            status: channel === "telegram" ? "sent" : "queued",
            tgSent: channel === "telegram",
          };
        }),
      );

      // Batch insert review_requests
      if (sendResults.length > 0) {
        await supabase.from("review_requests").insert(
          sendResults.map((r) => ({
            order_id: r.order_id,
            customer_id: r.customer_id,
            channel: r.channel,
            status: r.status,
          })),
        ).catch(() => {});
      }
      requested = sendResults.filter((r) => r.tgSent).length;
    }

    // ----- 2) Curate top reviews -----
    const { data: topReviews } = await supabase
      .from("reviews")
      .select("id, rating, text, author_name, created_at")
      .eq("is_published", true)
      .gte("rating", 5)
      .order("created_at", { ascending: false })
      .limit(20);

    const featureRows = (topReviews ?? [])
      .filter((r) => (r.text ?? "").length >= 50)
      .slice(0, 8)
      .map((r, idx) => ({
        review_id: r.id,
        rank: idx + 1,
        headline: (r.text ?? "").split(/[.!?]/)[0].slice(0, 80),
        highlight: (r.text ?? "").slice(0, 220),
        status: "featured",
      }));

    if (featureRows.length) {
      await supabase
        .from("ugc_features")
        .upsert(featureRows, { onConflict: "review_id" } as any);
    }

    if (requested > 0 || featureRows.length > 0) {
      await supabase.from("ai_insights").insert({
        insight_type: "ugc_harvest",
        title: `UGC: ${requested} запитів на відгук, ${featureRows.length} підсвічених`,
        description: `Надіслано ${requested} запитів на відгук після доставки. Виділено ${featureRows.length} топ-відгуків для соц-доказів.`,
        confidence: 0.7,
        risk_level: "low",
        affected_layer: "marketing",
        metrics: { review_requests_sent: requested, featured_reviews: featureRows.length },
      });
    }

    return new Response(
      JSON.stringify({ ok: true, requested, featured: featureRows.length }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e: any) {
    console.error("ugc-harvester error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
