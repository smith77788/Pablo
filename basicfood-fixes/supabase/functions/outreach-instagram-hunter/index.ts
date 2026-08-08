/**
 * 📷 Instagram Hashtag Harvester — публічний RSS/JSON hashtag scraping.
 *
 * Без офіційного Graph API. Стратегія:
 *  1) Спроба через INSTAGRAM_RSS_URL (RSS міст типу rsshub) якщо доступний.
 *  2) Інакше — публічна сторінка hashtag (часто блокується, ловимо обережно).
 *
 * Створює leads (channel=instagram, status=new) з draft підказкою для адміна.
 * Авто-постинг ВИМКНЕНО (за вимогою користувача).
 */
import {
  svcClient, getSettings, detectLanguage, scoreIntent, isBlocked,
  fingerprint, corsHeaders,
} from "../_shared/outreach.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const DEFAULT_HASHTAGS = [
  "кормдлясобак", "кормдлякотів", "корми_україна",
  "тваринирівне", "котирівне", "собакирівне",
  "petfoodua", "холістиккорм",
];

const RSS_BASE = Deno.env.get("INSTAGRAM_RSS_URL") ?? "";

interface IgPost {
  url: string;
  caption: string;
  hashtag: string;
  posted_at?: string;
}

/** Парсимо стандартний RSS Atom з полів title/link/description. */
function parseRss(xml: string, hashtag: string): IgPost[] {
  const out: IgPost[] = [];
  const itemRx = /<item>([\s\S]*?)<\/item>/g;
  let m: RegExpExecArray | null;
  while ((m = itemRx.exec(xml)) && out.length < 15) {
    const block = m[1];
    const link = (block.match(/<link>([\s\S]*?)<\/link>/) ?? [, ""])[1].trim();
    const title = (block.match(/<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/title>/) ?? [, ""])[1].trim();
    const desc = (block.match(/<description>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/description>/) ?? [, ""])[1]
      .replace(/<[^>]+>/g, " ").trim();
    const pub = (block.match(/<pubDate>([\s\S]*?)<\/pubDate>/) ?? [, ""])[1].trim();
    const caption = [title, desc].filter(Boolean).join(" — ").slice(0, 1500);
    if (!link || !caption) continue;
    out.push({ url: link, caption, hashtag, posted_at: pub || undefined });
  }
  return out;
}

async function fetchHashtag(tag: string): Promise<IgPost[]> {
  if (!RSS_BASE) return [];
  // Шаблон: rsshub приймає /instagram/tag/<tag>
  const url = `${RSS_BASE.replace(/\/+$/, "")}/instagram/tag/${encodeURIComponent(tag)}`;
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": "BasicFoodOutreach/1.0", Accept: "application/rss+xml,*/*" },
    });
    if (!res.ok) return [];
    const xml = await res.text();
    return parseRss(xml, tag);
  } catch { return []; }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;
  try {
    const body = await req.json().catch(() => ({}));
    const supabase = svcClient();
    const settings = await getSettings();
    if (!settings.active_channels.instagram) {
      return new Response(JSON.stringify({ ok: true, skipped: "instagram_inactive" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    if (!RSS_BASE) {
      return new Response(JSON.stringify({
        ok: true, skipped: "no_rss_bridge",
        hint: "Add INSTAGRAM_RSS_URL secret pointing to a public RSSHub instance.",
      }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
    }

    const tags = (await (async () => {
      const { data } = await supabase.from("outreach_settings").select("value").eq("key", "instagram_hashtags").maybeSingle();
      const v = data?.value;
      if (Array.isArray(v) && v.length) return v as string[];
      return DEFAULT_HASHTAGS;
    })());

    const sample = tags.sort(() => Math.random() - 0.5).slice(0, 3);
    let totalCreated = 0;
    let totalSeen = 0;
    const errors: string[] = [];

    // Collect qualified posts from all tags, then batch-dedup and batch-insert
    interface QualifiedPost { tag: string; p: any; lang: string; intent: { score: number; matched: string[] }; fp: string }
    const qualified: QualifiedPost[] = [];

    for (const tag of sample) {
      try {
        const posts = await fetchHashtag(tag);
        // Compute fingerprints in parallel for all posts in this tag
        const tagFiltered = posts.filter((p) => {
          totalSeen++;
          if (isBlocked(p.caption, settings.blocked_keywords)) return false;
          const lang = detectLanguage(p.caption);
          if (lang !== "uk" && lang !== "ru" && lang !== "en") return false;
          const intent = scoreIntent(p.caption, settings.intent_keywords);
          return intent.score >= 0.2;
        });
        const fps = await Promise.all(tagFiltered.map((p) => fingerprint("instagram", p.url, p.caption)));
        for (let i = 0; i < tagFiltered.length; i++) {
          const p = tagFiltered[i];
          const lang = detectLanguage(p.caption);
          const intent = scoreIntent(p.caption, settings.intent_keywords);
          qualified.push({ tag, p, lang, intent, fp: fps[i] });
        }
        await new Promise((r) => setTimeout(r, 700 + Math.random() * 800));
      } catch (e: any) {
        errors.push(`${tag}: ${e?.message ?? e}`);
      }
    }

    // Batch dedup check
    if (qualified.length > 0) {
      const allFps = qualified.map((q) => q.fp);
      const { data: existingFpRows } = await supabase
        .from("outreach_leads").select("fingerprint").in("fingerprint", allFps);
      const existingFps = new Set((existingFpRows ?? []).map((r: any) => r.fingerprint as string));

      const newLeadRows = qualified
        .filter((q) => !existingFps.has(q.fp))
        .map(({ tag, p, lang, intent, fp }) => ({
          channel: "instagram",
          source_url: p.url,
          title: `IG #${p.hashtag}`.slice(0, 280),
          content: p.caption.slice(0, 4000),
          language: lang,
          intent_score: intent.score,
          topic_tags: [`ig:#${p.hashtag}`],
          matched_keywords: intent.matched,
          fingerprint: fp,
          status: "new",
          raw_payload: { source: "ig_rss", hashtag: tag, posted_at: p.posted_at },
          discovered_at: p.posted_at ?? new Date().toISOString(),
        }));

      if (newLeadRows.length > 0) {
        const { error: insErr } = await supabase.from("outreach_leads").insert(newLeadRows);
        if (insErr) {
          if (insErr.code !== "23505") errors.push(insErr.message);
        } else { totalCreated = newLeadRows.length; }
      }
    }

    if (totalCreated > 0) {
      const url = `${Deno.env.get("SUPABASE_URL")}/functions/v1/outreach-composer`;
      const k = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${k}`, apikey: k },
        body: JSON.stringify({ initiated_by: "instagram-hunter", limit: 15 }),
      }).catch(() => {});
    }

    try {
      await supabase.from("agent_runs").insert({
        function_name: "outreach-instagram-hunter",
        trigger: detectTrigger(req, body),
        status: errors.length === 0 ? "success" : "partial",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `created=${totalCreated}, seen=${totalSeen}, tags=${sample.length}`,
        payload: { errors_sample: errors.slice(0, 3) },
      });
    } catch { /* ignore */ }

    return new Response(JSON.stringify({
      ok: true, tags_scanned: sample.length, seen: totalSeen,
      created: totalCreated, errors,
    }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
