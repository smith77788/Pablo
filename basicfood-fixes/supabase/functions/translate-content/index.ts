// Translate-content edge function.
// Takes a list of UA strings, returns EN translations.
// Caches results in `translations_cache` table to avoid re-billing AI on every page load.
//
// Request body:
//   { items: [{ key: string, text: string, namespace?: string }], targetLang: "en" }
// Response:
//   { translations: { [key]: string } }
//
// Notes:
//   - Source language is hardcoded to "ua" (the project default).
//   - Empty / whitespace-only strings pass through unchanged.
//   - On AI failure we return the original UA text (graceful degradation).
//
// Public function — no JWT required (used from anonymous public pages).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { routeAIText } from "../_shared/ai-router.ts";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? Deno.env.get("VITE_SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const sb = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

const enc = new TextEncoder();

async function sha256(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", enc.encode(s));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

interface Item {
  key: string;
  text: string;
  namespace?: string;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  const rl = rateLimit(`translate:${getClientIp(req)}`, { capacity: 20, refillPerSec: 0.3 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  try {
    const body = await req.json().catch(() => ({}));
    const items: Item[] = Array.isArray(body.items) ? body.items.slice(0, 200) : [];
    const targetLang: string = body.targetLang === "ua" ? "ua" : "en";
    const sourceLang = "ua";

    if (!items.length) {
      return new Response(JSON.stringify({ translations: {} }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // 1. Compute hashes + skip empty/short strings.
    const enriched = await Promise.all(
      items.map(async (it) => {
        const text = (it.text ?? "").toString();
        const ns = (it.namespace ?? "generic").slice(0, 64);
        const hash = await sha256(`${sourceLang}|${targetLang}|${ns}|${text}`);
        return { ...it, text, namespace: ns, hash };
      }),
    );

    const result: Record<string, string> = {};
    const passThrough = (it: typeof enriched[number]) => {
      if (!it.text.trim()) result[it.key] = it.text;
    };
    enriched.forEach(passThrough);

    const translatable = enriched.filter((i) => i.text.trim().length > 0);
    if (!translatable.length) {
      return new Response(JSON.stringify({ translations: result }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // 2. Look up cache.
    const hashes = translatable.map((i) => i.hash);
    const { data: cached } = await sb
      .from("translations_cache")
      .select("source_hash, translated_text")
      .eq("source_lang", sourceLang)
      .eq("target_lang", targetLang)
      .in("source_hash", hashes);

    const cacheMap = new Map<string, string>(
      (cached ?? []).map((r) => [r.source_hash, r.translated_text]),
    );

    const missing = translatable.filter((i) => !cacheMap.has(i.hash));
    translatable.forEach((i) => {
      if (cacheMap.has(i.hash)) result[i.key] = cacheMap.get(i.hash)!;
    });

    if (!missing.length) {
      return new Response(JSON.stringify({ translations: result }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // 3. Call AI for missing items in batches.
    const BATCH = 30;
    for (let i = 0; i < missing.length; i += BATCH) {
      const chunk = missing.slice(i, i + BATCH);
      const numbered = chunk.map((m, idx) => `[${idx}] ${m.text}`).join("\n---\n");

      const langName = targetLang === "en" ? "English" : "Ukrainian";
      const systemPrompt =
        `You are a professional ecommerce translator for BASIC.FOOD — a Ukrainian brand selling natural air-dried beef offal treats for dogs and cats. ` +
        `Translate each numbered Ukrainian source string into natural, marketing-quality ${langName}. ` +
        `Preserve emojis, HTML tags, line breaks, placeholders like {{name}}, currency symbols, numbers and product brand names exactly. ` +
        `Keep tone friendly, concise, B2C. ` +
        `Do NOT translate the brand name "BASIC.FOOD". ` +
        `Reply with ONLY the translations in the SAME numbered format ([0] translation\n---\n[1] translation ...), nothing else — no preamble, no markdown.`;

      let aiText = "";
      try {
        aiText = await routeAIText({
          model: "google/gemini-3-flash-preview",
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: numbered },
          ],
          temperature: 0.2,
          max_tokens: 4000,
          skipSanitize: true,
        });
      } catch (e) {
        console.error("translate-content ai error:", e);
        // Fallback: return source text unchanged
        chunk.forEach((m) => (result[m.key] = m.text));
        continue;
      }

      // Parse "[idx] translation" lines.
      const parsed = new Map<number, string>();
      const blocks = aiText.split(/\n---\n/);
      for (const block of blocks) {
        const m = block.match(/^\s*\[(\d+)\]\s*([\s\S]*?)\s*$/);
        if (m) parsed.set(Number(m[1]), m[2].trim());
      }

      // Persist to cache + populate result.
      const rows: Array<{
        source_lang: string;
        target_lang: string;
        namespace: string;
        source_hash: string;
        source_text: string;
        translated_text: string;
      }> = [];
      chunk.forEach((m, idx) => {
        const tr = parsed.get(idx);
        if (tr && tr.length) {
          result[m.key] = tr;
          rows.push({
            source_lang: sourceLang,
            target_lang: targetLang,
            namespace: m.namespace,
            source_hash: m.hash,
            source_text: m.text,
            translated_text: tr,
          });
        } else {
          // AI returned malformed output for this item
          result[m.key] = m.text;
        }
      });

      if (rows.length) {
        await sb
          .from("translations_cache")
          .upsert(rows, {
            onConflict: "source_lang,target_lang,namespace,source_hash",
            ignoreDuplicates: true,
          })
          .then(({ error }) => {
            if (error) console.error("translations_cache upsert error:", error);
          });
      }
    }

    return new Response(JSON.stringify({ translations: result }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("translate-content fatal:", e);
    return new Response(
      JSON.stringify({ error: e?.message ?? "Translation failed" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
