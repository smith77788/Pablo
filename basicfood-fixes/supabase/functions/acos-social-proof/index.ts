// ACOS Social Proof — returns aggregate counts for a product:
//   - purchases in last 24h (purchase_completed events containing this product)
//   - active viewers in last 5 min (distinct sessions with product_viewed)
// Public endpoint (no auth) — only returns aggregated, non-PII counts.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";

// PUBLIC endpoint: called from PDP (SocialProofBanner) for every product view.
// Returns only aggregated, non-PII counts for a single product_id.
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const rl = rateLimit(`social-proof:${getClientIp(req)}`, { capacity: 60, refillPerSec: 1 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  try {
    const url = new URL(req.url);
    const productId = url.searchParams.get("product_id");
    if (!productId || !UUID_RE.test(productId)) {
      return new Response(JSON.stringify({ error: "invalid product_id" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const since24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const since5m = new Date(Date.now() - 5 * 60 * 1000).toISOString();

    // Purchases in last 24h: count purchase_completed events that reference this product
    // (acos.ts emits purchase_completed once per checkout but order_items hold actual products,
    // so we also count distinct order_ids whose items include the product).
    const [{ data: itemRows }, { data: viewerRows }, { count: directViews }] = await Promise.all([
      supabase
        .from("order_items")
        .select("order_id, orders!inner(created_at, status)")
        .eq("product_id", productId)
        .gte("orders.created_at", since24h)
        .neq("orders.status", "cancelled")
        .limit(500),
      supabase
        .from("events")
        .select("session_id")
        .eq("event_type", "product_viewed")
        .eq("product_id", productId)
        .gte("created_at", since5m)
        .limit(500),
      supabase
        .from("events")
        .select("*", { count: "exact", head: true })
        .eq("event_type", "product_viewed")
        .eq("product_id", productId)
        .gte("created_at", since24h),
    ]);

    const distinctOrders = new Set((itemRows ?? []).map((r) => r.order_id));
    const distinctViewers = new Set((viewerRows ?? []).map((r) => r.session_id).filter(Boolean));

    const result = {
      product_id: productId,
      purchases_24h: distinctOrders.size,
      viewers_5m: distinctViewers.size,
      views_24h: directViews ?? 0,
      generated_at: new Date().toISOString(),
    };

    return new Response(JSON.stringify(result), {
      headers: {
        ...corsHeaders,
        "Content-Type": "application/json",
        "Cache-Control": "public, max-age=30",
      },
    });
  } catch (err) {
    console.error("acos-social-proof error", err);
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
