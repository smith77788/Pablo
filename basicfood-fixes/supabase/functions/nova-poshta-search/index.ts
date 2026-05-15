// Edge function — Nova Poshta address autocomplete proxy with in-memory cache.
// Public NP API works without an API key for read methods, but:
//   1) Browser CORS: we proxy server-side to keep `apikey: ""` private and
//      avoid the occasional CORS hiccup НП повертає на preflight.
//   2) Rate-limiting: per-instance LRU cache keeps repeat lookups (same city
//      typed by 100 users) at zero upstream cost.
//   3) Stable response shape — we project the bulky NP payload down to the
//      ~3 fields the UI actually needs (Description + Ref).
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const NP_URL = "https://api.novaposhta.ua/v2.0/json/";
// Public/free НП key for read methods. We could load from Deno.env.get
// but the public methods we use (getCities/getWarehouses) accept "" too.
const NP_KEY = Deno.env.get("NOVA_POSHTA_API_KEY") || "";

// LRU-ish cache: 5 min TTL, max 200 entries. Cities rarely change.
type CacheEntry = { at: number; data: unknown };
const cache = new Map<string, CacheEntry>();
const TTL_MS = 5 * 60_000;
const MAX_ENTRIES = 200;

const memo = (key: string): unknown | null => {
  const e = cache.get(key);
  if (!e) return null;
  if (Date.now() - e.at > TTL_MS) {
    cache.delete(key);
    return null;
  }
  // bump to MRU
  cache.delete(key);
  cache.set(key, e);
  return e.data;
};

const remember = (key: string, data: unknown) => {
  if (cache.size >= MAX_ENTRIES) {
    const first = cache.keys().next().value;
    if (first) cache.delete(first);
  }
  cache.set(key, { at: Date.now(), data });
};

interface NPCity {
  Description: string;
  Ref: string;
  AreaDescription?: string;
  SettlementTypeDescription?: string;
}

interface NPWarehouse {
  Description: string;
  ShortAddress: string;
  Ref: string;
  Number: string;
  CategoryOfWarehouse: string; // "Branch" | "Postomat"
  PlaceMaxWeightAllowed?: string;
}

async function searchCities(query: string): Promise<Array<{ ref: string; name: string; area: string }>> {
  const cacheKey = `cities:${query.toLowerCase()}`;
  const cached = memo(cacheKey);
  if (cached) return cached as never;

  const res = await fetch(NP_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      apiKey: NP_KEY,
      modelName: "AddressGeneral",
      calledMethod: "getCities",
      methodProperties: { FindByString: query, Limit: "10" },
    }),
  });
  const json = await res.json();
  const list = (json?.data as NPCity[] | undefined) || [];
  const projected = list.map((c) => ({
    ref: c.Ref,
    name: c.Description,
    area: c.AreaDescription || "",
  }));
  remember(cacheKey, projected);
  return projected;
}

async function searchWarehouses(
  cityName: string,
  query: string,
  type: "branch" | "parcel_locker",
): Promise<Array<{ ref: string; name: string; number: string; address: string }>> {
  const cacheKey = `wh:${type}:${cityName.toLowerCase()}:${query.toLowerCase()}`;
  const cached = memo(cacheKey);
  if (cached) return cached as never;

  // НП type: "Поштомат" категорія = Postomat. Branch = звичайне відділення.
  const typeOfWarehouseRef = type === "parcel_locker"
    ? "f9316480-5f2d-425d-bc2c-ac7cd29decf0" // Postomat refs
    : undefined;

  const props: Record<string, string> = {
    CityName: cityName,
    Limit: "30",
  };
  if (query) props.FindByString = query;
  if (typeOfWarehouseRef) props.TypeOfWarehouseRef = typeOfWarehouseRef;

  const res = await fetch(NP_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      apiKey: NP_KEY,
      modelName: "AddressGeneral",
      calledMethod: "getWarehouses",
      methodProperties: props,
    }),
  });
  const json = await res.json();
  const list = (json?.data as NPWarehouse[] | undefined) || [];
  const projected = list
    // Postomat-only filter is best-effort; NP sometimes returns mixed,
    // so we double-filter client side by category if needed.
    .filter((w) =>
      type === "parcel_locker"
        ? w.CategoryOfWarehouse === "Postomat"
        : w.CategoryOfWarehouse !== "Postomat",
    )
    .slice(0, 30)
    .map((w) => ({
      ref: w.Ref,
      name: w.Description,
      number: w.Number,
      address: w.ShortAddress,
    }));
  remember(cacheKey, projected);
  return projected;
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // Health-check
  if (req.method === "GET") {
    return new Response(JSON.stringify({ ok: true, cache_size: cache.size }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "method_not_allowed" }), {
      status: 405,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const rl = rateLimit(`np-search:${getClientIp(req)}`, { capacity: 100, refillPerSec: 2 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  try {
    // Tolerate empty/invalid body — return 400 instead of crashing with 500.
    let body: Record<string, unknown> = {};
    try {
      const raw = await req.text();
      if (raw && raw.trim().length > 0) body = JSON.parse(raw);
    } catch {
      return new Response(JSON.stringify({ error: "invalid_json" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const action = body?.action as "cities" | "warehouses" | undefined;
    const query: string = ((body?.query as string) || "").trim();

    if (!action) {
      return new Response(JSON.stringify({ error: "missing_action", expected: ["cities", "warehouses"] }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    if (action === "cities") {
      if (query.length < 2) {
        return new Response(JSON.stringify({ items: [] }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      const items = await searchCities(query);
      return new Response(JSON.stringify({ items }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    if (action === "warehouses") {
      const cityName: string = ((body?.cityName as string) || "").trim();
      const type: "branch" | "parcel_locker" = body?.type === "parcel_locker" ? "parcel_locker" : "branch";
      if (!cityName) {
        return new Response(JSON.stringify({ items: [] }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      const items = await searchWarehouses(cityName, query, type);
      return new Response(JSON.stringify({ items }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "unknown_action" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("nova-poshta-search error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
