/**
 * 🔎 Google/Blog Hunter — шукає UA-релевантні запити через DuckDuckGo HTML
 * (безкоштовно, без API), парсить сніпети, фільтрує по intent + блоклисту,
 * створює leads в `outreach_leads` (channel=google або blog).
 *
 * Безпечно для cron: query rotation, ліміт 25 leads / запит, дедуп через fingerprint.
 */
import {
  svcClient, getSettings, detectLanguage, scoreIntent, isBlocked,
  fingerprint, corsHeaders,
} from "../_shared/outreach.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
// UA-орієнтовані запити (можна винести в outreach_settings пізніше)
const DEFAULT_QUERIES = [
  "корм для собак рівне відгуки",
  "натуральний корм для котів україна порадьте",
  "чим годувати щеня форум україна",
  "беззерновий корм для собак купити",
  "корм холістик для котів думки",
  "перехід на сухий корм цуценя порада",
  "корм для стерилізованих кішок україна",
  "як вибрати корм для дрібних порід",
];

const DDG_URL = "https://html.duckduckgo.com/html/";

interface SearchResult {
  url: string;
  title: string;
  snippet: string;
}

async function ddgSearch(query: string): Promise<SearchResult[]> {
  // Try lite endpoint first (less aggressive bot detection from cloud IPs)
  const endpoints = [
    "https://lite.duckduckgo.com/lite/",
    "https://html.duckduckgo.com/html/",
  ];
  for (const ep of endpoints) {
    try {
      const body = new URLSearchParams({ q: query, kl: "ua-uk" });
      const res = await fetch(ep, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "User-Agent":
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
          "Accept": "text/html,application/xhtml+xml",
          "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
          "Referer": "https://duckduckgo.com/",
        },
        body: body.toString(),
      });
      if (!res.ok) continue;
      const html = await res.text();
      const results = ep.includes("/lite/") ? parseDdgLite(html) : parseDdgHtml(html);
      if (results.length) return results;
    } catch { /* try next */ }
  }
  return [];
}

function parseDdgHtml(html: string): SearchResult[] {
  const results: SearchResult[] = [];
  const rx = /<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>[\s\S]*?<a[^>]*class="result__snippet"[^>]*>([\s\S]*?)<\/a>/g;
  let m: RegExpExecArray | null;
  while ((m = rx.exec(html)) && results.length < 15) {
    let url = m[1];
    if (url.startsWith("//")) url = "https:" + url;
    try {
      const u = new URL(url);
      const real = u.searchParams.get("uddg");
      if (real) url = decodeURIComponent(real);
    } catch { /* ignore */ }
    const stripTags = (s: string) => s.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/gi, " ").trim();
    results.push({ url, title: stripTags(m[2]), snippet: stripTags(m[3]) });
  }
  return results;
}

function parseDdgLite(html: string): SearchResult[] {
  const results: SearchResult[] = [];
  // Lite: <a rel="nofollow" href="URL" class='result-link'>TITLE</a> ... <td class="result-snippet">SNIPPET
  const rx = /<a[^>]*class=['"]result-link['"][^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>[\s\S]*?<td[^>]*class=['"]result-snippet['"][^>]*>([\s\S]*?)<\/td>/g;
  let m: RegExpExecArray | null;
  while ((m = rx.exec(html)) && results.length < 15) {
    let url = m[1];
    if (url.startsWith("//")) url = "https:" + url;
    try {
      const u = new URL(url);
      const real = u.searchParams.get("uddg");
      if (real) url = decodeURIComponent(real);
    } catch { /* ignore */ }
    const stripTags = (s: string) => s.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/gi, " ").trim();
    results.push({ url, title: stripTags(m[2]), snippet: stripTags(m[3]) });
  }
  return results;
}

function classifyChannel(url: string): "blog" | "google" {
  // Якщо це reddit / форум-подібне → google; якщо блог/стаття → blog
  const blogHints = /(blog|статт|article|post|новин|news)/i;
  if (blogHints.test(url)) return "blog";
  return "google";
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
    if (!settings.active_channels.google && !settings.active_channels.blog) {
      return new Response(JSON.stringify({ ok: true, skipped: "channels_inactive" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const queries = DEFAULT_QUERIES.sort(() => Math.random() - 0.5).slice(0, 3);
    let totalCreated = 0;
    let totalSeen = 0;
    const errors: string[] = [];

    const stats = { lang_skip: 0, intent_skip: 0, blocked: 0, dup: 0 };

    // Collect qualified results from all queries, then batch-dedup and batch-insert
    interface QualifiedResult { q: string; r: any; channel: string; lang: string; intent: { score: number; matched: string[] }; fp: string }
    const qualified: QualifiedResult[] = [];

    for (const q of queries) {
      try {
        const results = await ddgSearch(q);
        const filtered: Array<{ r: any; channel: string; lang: string; intent: { score: number; matched: string[] } }> = [];
        for (const r of results) {
          totalSeen++;
          const text = `${r.title}\n${r.snippet}`;
          if (isBlocked(text, settings.blocked_keywords)) { stats.blocked++; continue; }
          const lang = detectLanguage(text);
          if (lang !== "uk" && lang !== "ru") { stats.lang_skip++; continue; }
          const intent = scoreIntent(text, settings.intent_keywords);
          if (intent.score < 0.15) { stats.intent_skip++; continue; }
          filtered.push({ r, channel: classifyChannel(r.url), lang, intent });
        }
        // Compute fingerprints for all filtered results in parallel
        const fps = await Promise.all(filtered.map(({ r, channel }) => fingerprint(channel, r.url, r.snippet)));
        for (let i = 0; i < filtered.length; i++) {
          qualified.push({ q, ...filtered[i], fp: fps[i] });
        }
        // дрібний джиттер між запитами щоб не дратувати DDG
        await new Promise((r) => setTimeout(r, 800 + Math.random() * 1200));
      } catch (e: any) {
        errors.push(`${q}: ${e?.message ?? e}`);
      }
    }

    // Batch dedup check + batch insert
    if (qualified.length > 0) {
      const allFps = qualified.map((q) => q.fp);
      const { data: existingFpRows } = await supabase
        .from("outreach_leads").select("fingerprint").in("fingerprint", allFps);
      const existingFps = new Set((existingFpRows ?? []).map((row: any) => row.fingerprint as string));

      const newLeadRows = qualified
        .filter((q) => !existingFps.has(q.fp))
        .map(({ q, r, channel, lang, intent, fp }) => ({
          channel,
          source_url: r.url,
          title: r.title.slice(0, 280),
          content: r.snippet.slice(0, 4000),
          language: lang,
          geo_country: "UA",
          intent_score: intent.score,
          topic_tags: [`q:${q.slice(0, 60)}`],
          matched_keywords: intent.matched,
          fingerprint: fp,
          status: "new",
          raw_payload: { source: "ddg", query: q },
        }));

      if (newLeadRows.length > 0) {
        const { error: insErr } = await supabase.from("outreach_leads").insert(newLeadRows);
        if (insErr) {
          if (insErr.code === "23505") stats.dup += newLeadRows.length;
          else errors.push(insErr.message);
        } else { totalCreated = newLeadRows.length; }
      }
    }

    // Trigger composer for new leads (best-effort)
    if (totalCreated > 0) {
      const url = `${Deno.env.get("SUPABASE_URL")}/functions/v1/outreach-composer`;
      const k = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${k}`, apikey: k },
        body: JSON.stringify({ initiated_by: "google-hunter", limit: 15 }),
      }).catch(() => {});
    }

    try {
      await supabase.from("agent_runs").insert({
        function_name: "outreach-google-hunter",
        trigger: detectTrigger(req, body),
        status: errors.length === 0 ? "success" : "partial",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `created=${totalCreated}, seen=${totalSeen}, queries=${queries.length}`,
        payload: { stats, errors_sample: errors.slice(0, 3) },
      });
    } catch { /* ignore */ }

    return new Response(JSON.stringify({
      ok: true, queries_run: queries.length, seen: totalSeen,
      created: totalCreated, stats, errors,
    }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
