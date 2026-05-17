/**
 * Reddit Hunter — публічний JSON-feed, без OAuth (read-only).
 * Сканує сабреддіти зі списку `outreach_settings.reddit_subreddits`,
 * підраховує intent, фільтрує блок-теми/мову, зберігає `outreach_leads`.
 *
 * Постинг тут НЕ виконуємо — то для outreach-action-executor (Stage 1+).
 * Запускається cron-ом щогодини або вручну з UI.
 */
import {
  corsHeaders, svcClient, getSettings,
  detectLanguage, scoreIntent, isBlocked, fingerprint,
  type OutreachChannel,
} from "../_shared/outreach.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
const REDDIT_USER_AGENT =
  Deno.env.get("REDDIT_USER_AGENT") ??
  "basic-food-bot/0.1 (+https://basic-food.shop)";

interface RedditPost {
  id: string;
  permalink: string;
  url: string;
  title: string;
  selftext: string;
  author: string;
  subreddit: string;
  created_utc: number;
  num_comments: number;
  ups: number;
}

async function fetchSubreddit(sub: string, limit = 25): Promise<RedditPost[]> {
  // Reddit JSON API повертає 403 для серверних IP без OAuth.
  // Альтернатива: RSS-фід — він рідше блокується anti-bot фільтром,
  // оскільки розрахований на читалки.
  const headersBrowser = {
    "User-Agent":
      "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
  };

  const attempts = [
    { url: `https://www.reddit.com/r/${encodeURIComponent(sub)}/new/.rss?limit=${limit}`, parser: "rss" as const },
    { url: `https://old.reddit.com/r/${encodeURIComponent(sub)}/new.json?limit=${limit}&raw_json=1`, parser: "json" as const },
    { url: `https://www.reddit.com/r/${encodeURIComponent(sub)}/new.json?limit=${limit}&raw_json=1`, parser: "json" as const },
  ];

  let lastStatus = 0;
  for (const a of attempts) {
    try {
      const res = await fetch(a.url, {
        headers: { ...headersBrowser, Accept: a.parser === "rss" ? "application/rss+xml,application/xml,text/xml" : "application/json" },
      });
      lastStatus = res.status;
      if (!res.ok) continue;

      if (a.parser === "json") {
        const json = await res.json().catch(() => null) as any;
        const children = json?.data?.children ?? [];
        if (!Array.isArray(children) || children.length === 0) continue;
        return children
          .map((c: any) => c?.data)
          .filter(Boolean)
          .map((d: any) => ({
            id: d.id,
            permalink: `https://www.reddit.com${d.permalink}`,
            url: d.url,
            title: d.title ?? "",
            selftext: d.selftext ?? "",
            author: d.author ?? "anon",
            subreddit: d.subreddit ?? sub,
            created_utc: d.created_utc ?? 0,
            num_comments: d.num_comments ?? 0,
            ups: d.ups ?? 0,
          }));
      }

      // RSS parsing — простий regex (Atom feed)
      const xml = await res.text();
      const entries = xml.match(/<entry>[\s\S]*?<\/entry>/g) ?? [];
      if (entries.length === 0) continue;
      const out: RedditPost[] = [];
      for (const e of entries) {
        const id = (e.match(/<id>(?:tag:reddit\.com,2008:)?(?:\/r\/[^/]+\/comments\/)?([^<]+)<\/id>/)?.[1] ?? "").split("_").pop() ?? "";
        const link = e.match(/<link[^>]*href="([^"]+)"/)?.[1] ?? "";
        const title = (e.match(/<title>([\s\S]*?)<\/title>/)?.[1] ?? "")
          .replace(/<!\[CDATA\[|\]\]>/g, "").trim();
        const author = e.match(/<name>\/u\/([^<]+)<\/name>/)?.[1] ?? "anon";
        // Контент часто в <content> з html-екранованим html
        const contentRaw = (e.match(/<content[^>]*>([\s\S]*?)<\/content>/)?.[1] ?? "")
          .replace(/<!\[CDATA\[|\]\]>/g, "");
        const content = contentRaw
          .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&quot;/g, '"')
          .replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
        const updated = e.match(/<updated>([^<]+)<\/updated>/)?.[1];
        const created_utc = updated ? Math.floor(new Date(updated).getTime() / 1000) : Math.floor(Date.now() / 1000);
        if (!id || !link) continue;
        out.push({
          id, permalink: link, url: link, title, selftext: content,
          author, subreddit: sub, created_utc, num_comments: 0, ups: 0,
        });
      }
      if (out.length) return out;
    } catch (_) { /* try next */ }
  }
  console.warn(`[reddit-hunter] r/${sub} → all variants failed, last status ${lastStatus}`);
  return [];
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
    const sb = svcClient();
    const settings = await getSettings();
    const channel: OutreachChannel = "reddit";

    if (!settings.active_channels.reddit) {
      return new Response(JSON.stringify({ ok: true, skipped: "channel_disabled" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const subreddits = settings.reddit_subreddits ?? [];
    if (subreddits.length === 0) {
      return new Response(JSON.stringify({ ok: true, skipped: "no_subreddits" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const stats = { scanned: 0, candidates: 0, inserted: 0, blocked: 0, lowIntent: 0, langSkip: 0, dup: 0 };
    const errors: string[] = [];

    for (const sub of subreddits) {
      try {
        const posts = await fetchSubreddit(sub, 25);
        stats.scanned += posts.length;

        // Filter posts synchronously, collect candidates.
        const isUaSub = /^(ukrain|lviv|kyiv|kiev|odesa|kharkiv)/i.test(sub);
        type Candidate = { p: RedditPost; text: string; lang: string; score: number; matched: string[] };
        const candidates: Candidate[] = [];

        for (const p of posts) {
          const text = `${p.title}\n\n${p.selftext}`.trim();
          if (!text) continue;
          if (isBlocked(text, settings.blocked_keywords)) { stats.blocked++; continue; }
          const lang = detectLanguage(text);
          if (lang !== "uk" && !(isUaSub && lang === "en")) { stats.langSkip++; continue; }
          const { score, matched } = scoreIntent(text, settings.intent_keywords);
          if (score < 0.25) { stats.lowIntent++; continue; }
          stats.candidates++;
          candidates.push({ p, text, lang, score, matched });
        }

        if (candidates.length === 0) continue;

        // Parallelize fingerprint computation, then batch upsert for this subreddit.
        const leadRows = await Promise.all(
          candidates.map(async ({ p, text, lang, score, matched }) => ({
            channel,
            source_url: p.permalink,
            source_platform_id: p.id,
            author_handle: p.author,
            author_url: `https://www.reddit.com/user/${p.author}`,
            title: p.title.slice(0, 280),
            content: text.slice(0, 4000),
            language: lang,
            geo_country: isUaSub ? "UA" : null,
            intent_score: score,
            topic_tags: [`r/${sub}`],
            matched_keywords: matched,
            fingerprint: await fingerprint("reddit", p.permalink, text),
            raw_payload: { sub, ups: p.ups, num_comments: p.num_comments, created_utc: p.created_utc },
            discovered_at: new Date(p.created_utc ? p.created_utc * 1000 : Date.now()).toISOString(),
          })),
        );

        const { data: inserted, error: insertErr } = await sb
          .from("outreach_leads")
          .upsert(leadRows, { onConflict: "fingerprint", ignoreDuplicates: true })
          .select("id");
        if (insertErr) {
          errors.push(`r/${sub}: ${insertErr.message}`);
        } else {
          stats.inserted += (inserted ?? []).length;
          stats.dup += leadRows.length - (inserted ?? []).length;
        }
      } catch (e) {
        errors.push(`r/${sub}: ${String((e as Error)?.message ?? e)}`);
      }
    }

    // Тригернути composer для свіжих leads (best-effort, не блокуємо відповідь)
    if (stats.inserted > 0) {
      const url = `${Deno.env.get("SUPABASE_URL")}/functions/v1/outreach-composer`;
      const k = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
      fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${k}`,
          apikey: k,
        },
        body: JSON.stringify({ initiated_by: "reddit-hunter", limit: 15 }),
      }).catch(() => {});
    }

    // Журнал агента (best-effort)
    try {
      await sb.from("agent_runs").insert({
        function_name: "outreach-reddit-hunter",
        trigger: detectTrigger(req, body),
        status: errors.length === 0 ? "success" : "partial",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `inserted=${stats.inserted}, dup=${stats.dup}, blocked=${stats.blocked}, low_intent=${stats.lowIntent}`,
        payload: { stats, errors_sample: errors.slice(0, 3) },
      });
    } catch { /* журнал не критичний */ }

    return new Response(JSON.stringify({ ok: true, stats, errors: errors.slice(0, 10) }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("[reddit-hunter] fatal:", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
