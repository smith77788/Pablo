import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAI } from "../_shared/ai-router.ts";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

// In-memory catalog cache — refreshed every 5 minutes.
// We store a compact snapshot so the system prompt stays under ~3KB.
let catalogCache: { fetchedAt: number; text: string; products: any[] } | null = null;
const CATALOG_TTL_MS = 5 * 60_000;

async function getCatalogContext(): Promise<{ text: string; products: any[] }> {
  if (catalogCache && Date.now() - catalogCache.fetchedAt < CATALOG_TTL_MS) {
    return catalogCache;
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
  const { data } = await supabase
    .from("products")
    .select("id, name, price, weight, categories, stock_quantity, description")
    .eq("is_active", true)
    .gt("stock_quantity", 0)
    .order("sold_count", { ascending: false })
    .limit(25);

  const products = data || [];
  // Compact one-line-per-product format. Saves ~60% tokens vs JSON.
  const text = products
    .map(
      (p: any) =>
        `- [${p.id}] ${p.name} (${p.weight || "—"}, ${p.price} грн, в наявності, категорії: ${(p.categories || []).join("/") || "—"})`,
    )
    .join("\n");

  catalogCache = { fetchedAt: Date.now(), text, products };
  return catalogCache;
}

function buildSystemPrompt(catalogText: string): string {
  return `Ти — досвідчений AI-консультант з продажів компанії BASIC.FOOD.
Ми виробляємо натуральні сушені яловичі ласощі для собак та котів.

ТВОЇ ЦІЛІ:
1. Допомогти клієнту обрати конкретний товар з нашого каталогу
2. Відповідати коротко (2-4 речення), українською мовою
3. Завжди рекомендувати конкретні товари ПО НАЗВІ зі списку нижче
4. Підводити до оформлення замовлення

ПРАВИЛА ВІДПОВІДІ:
- Якщо клієнт описує улюбленця або потребу — порадь 1-3 товари з каталогу і вкажи їх ID у форматі [PRODUCT:uuid]
- Не вигадуй товарів, яких немає у списку
- Не вигадуй цін — використовуй ті, що в каталозі
- Якщо запит не про ласощі — ввічливо поверни до теми
- Markdown дозволено: **жирний**, списки, посилання

АКТУАЛЬНИЙ КАТАЛОГ (топ-25 за продажами, тільки в наявності):
${catalogText}

ПРИКЛАД ВІДПОВІДІ:
"Для маленької собачки 5 кг чудово підійдуть наші легені — м'які та легко жуються:
[PRODUCT:abc-123-def]
А для дресирування рекомендую яловиче серце [PRODUCT:xyz-456-ghi] — нарізане кубиками."`;
}

const GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models";
const GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"];

async function geminiChat(apiKey: string, systemPrompt: string, history: any[]): Promise<string> {
  let lastErr: Error | null = null;
  const contents = history.slice(-10).map((m: any) => ({
    role: m.role === "assistant" ? "model" : "user",
    parts: [{ text: String(m.content || "") }],
  }));
  for (const model of GEMINI_MODELS) {
    try {
      const url = `${GEMINI_BASE}/${model}:generateContent?key=${apiKey}`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: systemPrompt }] },
          contents,
          generationConfig: { temperature: 0.7, maxOutputTokens: 1024 },
        }),
      });
      if (!res.ok) {
        const t = await res.text();
        console.log(`Gemini ${model} failed [${res.status}]: ${t.slice(0, 200)}`);
        lastErr = new Error(`Gemini ${model} ${res.status}`);
        continue;
      }
      const data = await res.json();
      const text =
        data?.candidates?.[0]?.content?.parts?.map((p: any) => p.text).filter(Boolean).join("\n") || "";
      if (!text) {
        lastErr = new Error("Empty Gemini response");
        continue;
      }
      return text;
    } catch (e) {
      lastErr = e as Error;
    }
  }
  throw lastErr || new Error("All Gemini models failed");
}

async function independentChat(systemPrompt: string, messages: any[]): Promise<string> {
  const result = await routeAI({
    model: "google/gemini-3-flash-preview",
    messages: [{ role: "system", content: systemPrompt }, ...messages.slice(-10)],
    noCache: true,
    skipLovable: true,
    timeoutMs: 25_000,
  });
  return result.content || "";
}

// Extract [PRODUCT:uuid] markers from AI reply and resolve them against catalog.
function extractRecommendedProducts(reply: string, catalogProducts: any[]) {
  const ids = new Set<string>();
  const regex = /\[PRODUCT:([a-f0-9-]{36})\]/gi;
  let match;
  while ((match = regex.exec(reply)) !== null) {
    ids.add(match[1].toLowerCase());
  }
  if (ids.size === 0) return [];
  return catalogProducts
    .filter((p) => ids.has(String(p.id).toLowerCase()))
    .map((p) => ({ id: p.id, name: p.name, price: p.price, weight: p.weight }));
}

// (Lovable streaming removed — agents must be credit-independent.
//  Non-stream path below uses Gemini direct → routeAI cascade.)

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // AI is the most expensive endpoint we expose — anti-abuse cap.
  // ≈12 req/min sustained, bursts up to 20.
  const rl = rateLimit(`ai-chat:${getClientIp(req)}`, { capacity: 20, refillPerSec: 0.2 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  try {
    const url = new URL(req.url);
    const wantStream = url.searchParams.get("stream") === "1";
    const { messages } = await req.json();

    if (!Array.isArray(messages) || messages.length === 0) {
      return new Response(JSON.stringify({ error: "messages array required" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const catalog = await getCatalogContext();
    const systemPrompt = buildSystemPrompt(catalog.text);

    const GEMINI_API_KEY = Deno.env.get("GEMINI_API_KEY");

    // Streaming branch removed — independent providers may not all support OpenAI-SSE.
    // Non-stream path: Gemini direct → routeAI cascade (Together/Cohere/Groq/OpenRouter).
    void wantStream;

    let reply = "";

    if (GEMINI_API_KEY) {
      try {
        reply = await geminiChat(GEMINI_API_KEY, systemPrompt, messages);
      } catch (e) {
        console.log("Gemini direct failed:", (e as Error).message);
      }
    }
    if (!reply) {
      try {
        reply = await independentChat(systemPrompt, messages);
      } catch (e) {
        console.log("Independent AI router failed:", (e as Error).message);
      }
    }
    if (!reply) reply = "Вибачте, не вдалося згенерувати відповідь. Спробуйте ще раз.";

    const recommendedProducts = extractRecommendedProducts(reply, catalog.products);
    // Strip [PRODUCT:uuid] markers from visible reply — UI renders cards instead.
    const visibleReply = reply.replace(/\s*\[PRODUCT:[a-f0-9-]{36}\]\s*/gi, " ").trim();

    return new Response(
      JSON.stringify({ reply: visibleReply, recommendedProducts }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    console.error("ai-chat error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
