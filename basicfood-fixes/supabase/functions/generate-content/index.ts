import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { generateImageCascade } from "../_shared/image-providers.ts";
import { requireInternalCaller } from "../_shared/auth.ts";


const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

// Direct Google Gemini (free tier — independent of Lovable credits).
const GEMINI_TEXT_MODELS = [
  "gemini-2.5-flash",
  "gemini-2.0-flash",
  "gemini-1.5-flash",
];
const IMAGEN_MODELS = [
  "imagen-3.0-generate-002",
  "imagen-4.0-generate-preview-06-06",
];

const GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models";

// ---------- Direct Google Gemini ----------
async function geminiText(apiKey: string, models: string[], systemPrompt: string, userPrompt: string): Promise<string> {
  let lastErr: Error | null = null;
  for (const model of models) {
    try {
      const url = `${GEMINI_BASE}/${model}:generateContent?key=${apiKey}`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: systemPrompt }] },
          contents: [{ role: "user", parts: [{ text: userPrompt }] }],
          generationConfig: { temperature: 0.9, maxOutputTokens: 2048 },
        }),
      });
      if (!res.ok) {
        const t = await res.text();
        console.log(`Gemini text ${model} failed [${res.status}]: ${t.slice(0, 200)}`);
        lastErr = new Error(`Gemini ${model} ${res.status}`);
        continue;
      }
      const data = await res.json();
      const text = data?.candidates?.[0]?.content?.parts?.map((p: any) => p.text).filter(Boolean).join("\n") || "";
      if (!text) { lastErr = new Error("Empty Gemini response"); continue; }
      return text;
    } catch (e) {
      console.log(`Gemini text exception ${model}:`, e);
      lastErr = e as Error;
    }
  }
  throw lastErr || new Error("All Gemini text models failed");
}

async function geminiImage(apiKey: string, models: string[], prompt: string): Promise<{ b64: string; text: string }> {
  let lastErr: Error | null = null;
  // Gemini image-out models
  for (const model of models) {
    try {
      const url = `${GEMINI_BASE}/${model}:generateContent?key=${apiKey}`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: prompt }] }],
          generationConfig: { responseModalities: ["IMAGE", "TEXT"] },
        }),
      });
      if (!res.ok) {
        const t = await res.text();
        console.log(`Gemini image ${model} failed [${res.status}]: ${t.slice(0, 200)}`);
        lastErr = new Error(`Gemini image ${model} ${res.status}`);
        continue;
      }
      const data = await res.json();
      const parts = data?.candidates?.[0]?.content?.parts || [];
      let b64 = "";
      let text = "";
      for (const p of parts) {
        if (p?.inlineData?.data) b64 = p.inlineData.data;
        if (p?.text) text += p.text;
      }
      if (!b64) { lastErr = new Error("No image in Gemini response"); continue; }
      return { b64, text };
    } catch (e) {
      console.log(`Gemini image exception ${model}:`, e);
      lastErr = e as Error;
    }
  }
  // Imagen fallback (much higher free quota)
  for (const model of IMAGEN_MODELS) {
    try {
      const url = `${GEMINI_BASE}/${model}:predict?key=${apiKey}`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instances: [{ prompt }],
          parameters: { sampleCount: 1, aspectRatio: "1:1" },
        }),
      });
      if (!res.ok) {
        const t = await res.text();
        console.log(`Imagen ${model} failed [${res.status}]: ${t.slice(0, 200)}`);
        lastErr = new Error(`Imagen ${model} ${res.status}`);
        continue;
      }
      const data = await res.json();
      const b64 = data?.predictions?.[0]?.bytesBase64Encoded;
      if (b64) return { b64, text: "" };
      lastErr = new Error("No image in Imagen response");
    } catch (e) {
      console.log(`Imagen exception ${model}:`, e);
      lastErr = e as Error;
    }
  }
  throw lastErr || new Error("All image models failed");
}

// (Lovable AI fallback removed — agents must be credit-independent.)

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const GEMINI_API_KEY = Deno.env.get("GEMINI_API_KEY");
    if (!GEMINI_API_KEY) throw new Error("GEMINI_API_KEY not configured (independent AI required)");

    const { action, product_name, product_description, style, language } = await req.json();

    if (action === "generate_text") {
      const systemPrompt = `Ти — експерт з маркетингу та копірайтингу для Instagram та Telegram. 
Бренд: BASIC.FOOD — натуральні сушені яловичі ласощі для собак та котів.
Мова: ${language || "українська"}.
Стиль: ${style || "продаючий, емоційний, з емодзі"}.

Створюй контент що продає: заголовки, основний текст, хештеги.
Формат відповіді — JSON:
{
  "instagram_post": "текст для Instagram поста з хештегами",
  "instagram_story": "короткий текст для Stories",
  "telegram_broadcast": "текст для розсилки в Telegram боті (HTML-форматування: <b>, <i>, <a>)",
  "hashtags": ["масив", "хештегів"]
}`;
      const userPrompt = `Створи продаючий контент для продукту: ${product_name || "сушені яловичі ласощі"}. ${product_description || "Натуральний склад, без консервантів, для собак та котів."}`;

      let content = "";
      let provider = "";
      // Try Gemini direct first (unlimited free tier)
      if (GEMINI_API_KEY) {
        try {
          content = await geminiText(GEMINI_API_KEY, GEMINI_TEXT_MODELS, systemPrompt, userPrompt);
          provider = "gemini-direct";
        } catch (e) {
          console.log("Gemini direct failed, falling back to Lovable AI:", (e as Error).message);
        }
      }
      // Independent fallback: routeAI (Together / Cohere / Groq / OpenRouter)
      if (!content) {
        try {
          const { routeAI } = await import("../_shared/ai-router.ts");
          const r = await routeAI({
            model: "google/gemini-2.5-flash",
            messages: [
              { role: "system", content: systemPrompt },
              { role: "user", content: userPrompt },
            ],
            skipLovable: true,
            timeoutMs: 25_000,
          });
          content = r.content || "";
          provider = r.provider;
        } catch (e) {
          console.log("routeAI fallback failed:", (e as Error).message);
        }
      }
      if (!content) throw new Error("All independent AI providers failed");

      let parsed: any = null;
      try {
        const jsonMatch = content.match(/\{[\s\S]*\}/);
        if (jsonMatch) parsed = JSON.parse(jsonMatch[0]);
      } catch {
        parsed = { instagram_post: content, telegram_broadcast: content, hashtags: [] };
      }
      if (!parsed) parsed = { instagram_post: content, telegram_broadcast: content, hashtags: [] };

      return new Response(JSON.stringify({ ok: true, content: parsed, raw: content, provider }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    if (action === "generate_image") {
      const prompt = `Create a professional, appetizing product photo for Instagram. 
Product: ${product_name || "dried beef treats for pets"} by BASIC.FOOD brand.
Style: ${style || "warm golden tones, dark background, premium feel, pet food photography"}.
${product_description || "Natural dried beef jerky treats for dogs and cats."}
Make it look like a professional food photo for social media marketing.`;

      const HF_TOKEN = Deno.env.get("HF_TOKEN") || undefined;
      const TOGETHER_API_KEY = Deno.env.get("TOGETHER_API_KEY") || undefined;
      const img = await generateImageCascade({
        prompt,
        aspect: "1:1",
        geminiKey: GEMINI_API_KEY,
        hfToken: HF_TOKEN,
        togetherKey: TOGETHER_API_KEY,
        // lovableKey intentionally omitted — agents must be credit-independent
      });

      const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
      const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
      const supabase = createClient(supabaseUrl, supabaseKey);
      const ext = img.contentType.includes("jpeg") ? "jpg" : "png";
      const path = `generated/${Date.now()}.${ext}`;

      const { error: uploadErr } = await supabase.storage
        .from("broadcast-media")
        .upload(path, img.bytes, { contentType: img.contentType, upsert: true });
      if (uploadErr) throw new Error(`Upload failed: ${uploadErr.message}`);

      const { data: urlData } = supabase.storage.from("broadcast-media").getPublicUrl(path);

      return new Response(
        JSON.stringify({
          ok: true,
          image_url: urlData.publicUrl,
          description: "",
          provider: img.provider,
          model: img.model,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    return new Response(JSON.stringify({ error: "Unknown action" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err: any) {
    console.error("generate-content error:", err);
    return new Response(
      JSON.stringify({ error: err.message || "Internal error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
