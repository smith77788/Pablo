/**
 * 📲 Telegram Public Hunter — парсить публічні t.me/s/<channel> preview сторінки
 * тематичних каналів/чатів, шукає сигнали про корм, дієту, годування тварин,
 * створює leads (channel=telegram). БЕЗ використання Bot API — лише публічний HTML.
 *
 * Постинг недоступний (читання по preview ≠ участь у чаті). Використовується
 * для генерації drafts → ручне публікування адміном з /admin/outreach.
 */
import {
  svcClient, getSettings, detectLanguage, scoreIntent, isBlocked,
  fingerprint, corsHeaders,
} from "../_shared/outreach.ts";
import { detectTrigger } from "../_shared/agent-logger.ts";

import { requireInternalCaller } from "../_shared/auth.ts";
// Дефолтний список UA-каналів про тварин (можна винести в outreach_settings).
const DEFAULT_CHANNELS = [
  "dog_ua", "kotyaty", "vetmedicineua", "korm_ua", "petfood_ua",
  "rivneanimal", "rivne_pets", "rescue_ua",
];

interface TgPost {
  url: string;
  text: string;
  date: string | null;
  channel: string;
}

interface TelegramSignal {
  sourceUrl: string;
  sourcePlatformId: string;
  authorHandle: string;
  title: string;
  content: string;
  language: string;
  geoCountry: string | null;
  intentScore: number;
  matchedKeywords: string[];
  topicTags: string[];
  discoveredAt: string;
  rawPayload: Record<string, unknown>;
}

function boostTelegramIntent(text: string, baseScore: number): number {
  let score = baseScore;
  if (/(чим годувати|який корм|що дати|що краще|порадьте|порекомендуйте|де купити|доставка|ціна|вартість)/i.test(text)) score += 0.18;
  if (/(ласощ|сушен|м'яс|мяс|смаколик|корм|раціон|натурал)/i.test(text)) score += 0.14;
  if (/(собак|пес|цуцен|щен|кіт|кот|кошен|тварин|pet|dog|cat)/i.test(text)) score += 0.1;
  if (text.length > 120) score += 0.05;
  return Math.max(0, Math.min(1, Number(score.toFixed(3))));
}

async function fetchChannel(channel: string, maxPosts: number): Promise<TgPost[]> {
  const url = `https://t.me/s/${channel}`;
  const res = await fetch(url, {
    headers: {
      "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    },
  });
  if (!res.ok) return [];
  const html = await res.text();
  const posts: TgPost[] = [];
  // Зібрати всі message_text блоки + знайти найближчий data-post перед ними.
  const postIds: { idx: number; id: string }[] = [];
  const postRx = /data-post="([^"]+)"/g;
  let pm: RegExpExecArray | null;
  while ((pm = postRx.exec(html))) postIds.push({ idx: pm.index, id: pm[1] });

  const textRx = /tgme_widget_message_text[^>]*>([\s\S]*?)<\/div>/g;
  const dateRx = /<time[^>]*datetime="([^"]+)"/g;
  const dates: { idx: number; ts: string }[] = [];
  let dm: RegExpExecArray | null;
  while ((dm = dateRx.exec(html))) dates.push({ idx: dm.index, ts: dm[1] });

  let m: RegExpExecArray | null;
  while ((m = textRx.exec(html)) && posts.length < maxPosts) {
    const idx = m.index;
    // Знайти найближчий data-post ПЕРЕД цим текстом
    const post = [...postIds].reverse().find((p) => p.idx < idx);
    if (!post) continue;
    const date = dates.find((d) => d.idx > idx);
    const postId = post.id;
    const rawText = m[1];
    const text = rawText
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/gi, " ")
      .replace(/&amp;/gi, "&")
      .replace(/&lt;/gi, "<")
      .replace(/&gt;/gi, ">")
      .replace(/&quot;/gi, '"')
      .trim();
    if (!text || text.length < 20) continue;
    posts.push({
      url: `https://t.me/${postId}`,
      text,
      date: date?.ts ?? null,
      channel,
    });
  }
  return posts;
}

async function collectPublicChannelSignals(
  settings: Awaited<ReturnType<typeof getSettings>>,
  channels: string[],
  maxPosts: number,
  minIntentScore: number,
) {
  const signals: TelegramSignal[] = [];
  const errors: string[] = [];
  let seen = 0;

  for (const ch of channels) {
    try {
      const posts = await fetchChannel(ch, maxPosts);
      for (const p of posts) {
        seen++;
        if (isBlocked(p.text, settings.blocked_keywords)) continue;

        const lang = detectLanguage(p.text);
        if (!["uk", "ru", "en"].includes(lang)) continue;

        const baseIntent = scoreIntent(p.text, settings.intent_keywords);
        const finalScore = boostTelegramIntent(p.text, baseIntent.score);
        if (finalScore < minIntentScore) continue;

        signals.push({
          sourceUrl: p.url,
          sourcePlatformId: p.url.split("/").pop() ?? `${p.channel}_${seen}`,
          authorHandle: `@${p.channel}`,
          title: `Публікація в Telegram: @${p.channel}`.slice(0, 280),
          content: p.text.slice(0, 4000),
          language: lang,
          geoCountry: "UA",
          intentScore: finalScore,
          matchedKeywords: baseIntent.matched,
          topicTags: [`tg:${p.channel}`, "telegram_public"],
          discoveredAt: p.date ?? new Date().toISOString(),
          rawPayload: { source: "tg_public", channel: p.channel, posted_at: p.date },
        });
      }
      await new Promise((r) => setTimeout(r, 500 + Math.random() * 500));
    } catch (e: any) {
      errors.push(`${ch}: ${e?.message ?? e}`);
    }
  }

  return { signals, errors, seen };
}

async function collectInternalChatSignals(
  settings: Awaited<ReturnType<typeof getSettings>>,
  lookbackDays: number,
  minIntentScore: number,
) {
  const supabase = svcClient();
  const since = new Date(Date.now() - lookbackDays * 24 * 3600 * 1000).toISOString();
  const { data: messages, error } = await supabase
    .from("telegram_messages")
    .select("id, chat_id, message_text, created_at, sender_name")
    .eq("direction", "in")
    .gte("created_at", since)
    .not("message_text", "is", null)
    .order("created_at", { ascending: false })
    .limit(250);

  if (error) throw new Error(error.message);

  const seenChats = new Set<number>();
  const signals: TelegramSignal[] = [];

  for (const msg of messages ?? []) {
    const text = String(msg.message_text ?? "").trim();
    const chatId = Number(msg.chat_id);
    if (!text || seenChats.has(chatId) || isBlocked(text, settings.blocked_keywords)) continue;

    const lang = detectLanguage(text);
    if (!["uk", "ru", "en"].includes(lang)) continue;

    const baseIntent = scoreIntent(text, settings.intent_keywords);
    const finalScore = boostTelegramIntent(text, baseIntent.score);
    if (finalScore < minIntentScore) continue;

    seenChats.add(chatId);
    signals.push({
      sourceUrl: `${settings.default_landing.url.replace(/\/$/, "")}/admin/inbox?chat=${chatId}`,
      sourcePlatformId: `chat_${chatId}`,
      authorHandle: msg.sender_name ? `${msg.sender_name}` : `Чат ${chatId}`,
      title: `Живий запит у Telegram-чаті #${chatId}`.slice(0, 280),
      content: text.slice(0, 4000),
      language: lang,
      geoCountry: "UA",
      intentScore: finalScore,
      matchedKeywords: baseIntent.matched,
      topicTags: ["telegram_inbox", "telegram_customer"],
      discoveredAt: msg.created_at,
      rawPayload: {
        source: "tg_inbox",
        chat_id: chatId,
        message_id: msg.id,
        sender_name: msg.sender_name ?? null,
        received_at: msg.created_at,
      },
    });
  }

  return { signals, seen: (messages ?? []).length };
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
    if (!settings.active_channels.telegram) {
      return new Response(JSON.stringify({ ok: true, skipped: "telegram_inactive" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const configuredChannels = settings.telegram_channels?.length ? settings.telegram_channels : DEFAULT_CHANNELS;
    const channels = configuredChannels.slice(0, Math.max(1, Number(settings.telegram_max_channels_per_run ?? 10)));
    const minIntentScore = Math.max(0.05, Number(settings.telegram_min_intent_score ?? 0.22));
    const maxPosts = Math.max(5, Number(settings.telegram_max_posts_per_channel ?? 35));
    const lookbackDays = Math.max(1, Number(settings.telegram_internal_lookback_days ?? 21));

    const errors: string[] = [];
    const stats = {
      public_channels_scanned: channels.length,
      public_seen: 0,
      inbox_seen: 0,
      created: 0,
      duplicates: 0,
    };

    const [publicSignals, inboxSignals] = await Promise.all([
      collectPublicChannelSignals(settings, channels, maxPosts, minIntentScore),
      collectInternalChatSignals(settings, lookbackDays, minIntentScore),
    ]);

    errors.push(...publicSignals.errors);
    stats.public_seen = publicSignals.seen;
    stats.inbox_seen = inboxSignals.seen;

    const mergedSignals = [...inboxSignals.signals, ...publicSignals.signals]
      .sort((a, b) => b.intentScore - a.intentScore)
      .slice(0, 120);

    // Compute all fingerprints in parallel, then batch-dedup in one query
    const signalFps = await Promise.all(
      mergedSignals.map((s) => fingerprint("telegram", s.sourceUrl, s.content)),
    );
    const { data: existingFpRows } = await supabase
      .from("outreach_leads").select("fingerprint").in("fingerprint", signalFps);
    const existingFps = new Set((existingFpRows ?? []).map((r: any) => r.fingerprint as string));

    const newLeadRows: any[] = [];
    for (let i = 0; i < mergedSignals.length; i++) {
      const signal = mergedSignals[i];
      const fp = signalFps[i];
      if (existingFps.has(fp)) { stats.duplicates++; continue; }
      newLeadRows.push({
        channel: "telegram",
        source_url: signal.sourceUrl,
        source_platform_id: signal.sourcePlatformId,
        author_handle: signal.authorHandle,
        title: signal.title,
        content: signal.content,
        language: signal.language,
        geo_country: signal.geoCountry,
        intent_score: signal.intentScore,
        topic_tags: signal.topicTags,
        matched_keywords: signal.matchedKeywords,
        fingerprint: fp,
        status: "new",
        raw_payload: signal.rawPayload,
        discovered_at: signal.discoveredAt,
      });
    }

    if (newLeadRows.length > 0) {
      const { error: insErr } = await supabase.from("outreach_leads").insert(newLeadRows);
      if (insErr) {
        if (insErr.code === "23505") stats.duplicates += newLeadRows.length;
        else errors.push(insErr.message);
      } else {
        stats.created = newLeadRows.length;
      }
    }

    if (stats.created > 0) {
      const url = `${Deno.env.get("SUPABASE_URL")}/functions/v1/outreach-composer`;
      const k = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${k}`, apikey: k },
        body: JSON.stringify({ initiated_by: "telegram-hunter", limit: 25 }),
      }).catch(() => {});
    }

    try {
      await supabase.from("agent_runs").insert({
        function_name: "outreach-telegram-hunter",
        trigger: detectTrigger(req, body),
        status: errors.length === 0 ? "success" : "partial",
        started_at: new Date(Date.now() - 5000).toISOString(),
        finished_at: new Date().toISOString(),
        summary: `created=${stats.created}, public_seen=${stats.public_seen}, inbox_seen=${stats.inbox_seen}, channels=${channels.length}`,
        payload: { errors_sample: errors.slice(0, 3) },
      });
    } catch { /* ignore */ }

    return new Response(JSON.stringify({
      ok: true,
      channels_scanned: channels.length,
      stats,
      errors,
    }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
