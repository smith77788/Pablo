// Instagram sync via RSS bridge (rss.app) — no Meta Developer needed.
//
// Required secret:
//   INSTAGRAM_RSS_URL — full RSS feed URL from rss.app
//
// Triggered by:
//   - Manual call from admin (POST)
//   - Scheduled cron (every 6h)

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

interface ParsedItem {
  ig_id: string;
  permalink: string;
  caption: string | null;
  media_url: string | null;
  thumbnail_url: string | null;
  media_type: "IMAGE" | "VIDEO";
  timestamp: string;
}

// --- Tiny XML helpers (no deps) ---
function decodeEntities(s: string): string {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function stripCdata(s: string): string {
  const m = s.match(/^<!\[CDATA\[([\s\S]*?)\]\]>$/);
  return m ? m[1] : s;
}

function getTag(block: string, tag: string): string | null {
  const re = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i");
  const m = block.match(re);
  if (!m) return null;
  return decodeEntities(stripCdata(m[1].trim()));
}

function getAttr(block: string, tag: string, attr: string): string | null {
  const re = new RegExp(`<${tag}[^>]*\\b${attr}=["']([^"']+)["']`, "i");
  const m = block.match(re);
  return m ? decodeEntities(m[1]) : null;
}

function extractItems(xml: string): string[] {
  const items: string[] = [];
  const re = /<item\b[\s\S]*?<\/item>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(xml)) !== null) items.push(m[0]);
  return items;
}

// Pull <img src="..."> from HTML (description / content:encoded)
function firstImgFromHtml(html: string): string | null {
  const m = html.match(/<img[^>]+src=["']([^"']+)["']/i);
  return m ? m[1] : null;
}

// Pull <video src="..."> or .mp4 link from HTML
function firstVideoFromHtml(html: string): string | null {
  const v = html.match(/<video[^>]+src=["']([^"']+)["']/i);
  if (v) return v[1];
  const link = html.match(/href=["']([^"']+\.mp4[^"']*)["']/i);
  return link ? link[1] : null;
}

function ig_id_from_link(link: string): string {
  // https://www.instagram.com/p/ABC123/  -> ABC123
  const m = link.match(/\/(?:p|reel|tv)\/([^/?#]+)/);
  return m ? m[1] : link;
}

function parseFeed(xml: string): ParsedItem[] {
  const items = extractItems(xml);
  const out: ParsedItem[] = [];

  for (const block of items) {
    const link = getTag(block, "link") ?? "";
    if (!link) continue;

    const title = getTag(block, "title") ?? "";
    const description = getTag(block, "description") ?? "";
    const contentEncoded =
      getTag(block, "content:encoded") ?? getTag(block, "content") ?? "";
    const pubDate = getTag(block, "pubDate") ?? "";

    // rss.app exposes media in <enclosure url="..." type="image/jpeg"/> and/or
    // inside HTML in <description>/<content:encoded>.
    const enclosureUrl = getAttr(block, "enclosure", "url");
    const enclosureType = getAttr(block, "enclosure", "type") ?? "";
    const mediaContentUrl = getAttr(block, "media:content", "url");
    const mediaContentType = getAttr(block, "media:content", "type") ?? "";
    const mediaThumbUrl = getAttr(block, "media:thumbnail", "url");

    const html = `${description}\n${contentEncoded}`;
    const htmlImg = firstImgFromHtml(html);
    const htmlVideo = firstVideoFromHtml(html);

    let media_type: "IMAGE" | "VIDEO" = "IMAGE";
    let media_url: string | null = null;
    let thumbnail_url: string | null = null;

    if (
      htmlVideo ||
      enclosureType.startsWith("video") ||
      mediaContentType.startsWith("video")
    ) {
      media_type = "VIDEO";
      media_url =
        htmlVideo ??
        (enclosureType.startsWith("video") ? enclosureUrl : null) ??
        (mediaContentType.startsWith("video") ? mediaContentUrl : null);
      thumbnail_url = mediaThumbUrl ?? htmlImg ?? enclosureUrl ?? null;
    } else {
      media_type = "IMAGE";
      media_url =
        htmlImg ?? enclosureUrl ?? mediaContentUrl ?? mediaThumbUrl ?? null;
      thumbnail_url = mediaThumbUrl ?? media_url;
    }

    let timestamp = new Date().toISOString();
    if (pubDate) {
      const d = new Date(pubDate);
      if (!isNaN(d.getTime())) timestamp = d.toISOString();
    }

    // Caption: prefer description text without HTML tags, fall back to title.
    const captionRaw = description || title;
    const caption = captionRaw
      ? captionRaw.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim() || null
      : null;

    out.push({
      ig_id: ig_id_from_link(link),
      permalink: link,
      caption,
      media_url,
      thumbnail_url,
      media_type,
      timestamp,
    });
  }

  return out;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const RSS_URL = Deno.env.get("INSTAGRAM_RSS_URL");
  const SUPABASE_URL = Deno.env.get("SUPABASE_URL");
  const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  if (!SUPABASE_URL || !SERVICE_KEY) {
    return new Response(
      JSON.stringify({ error: "Supabase env not configured" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  if (!RSS_URL) {
    return new Response(
      JSON.stringify({
        ok: false,
        buffered: true,
        message:
          "INSTAGRAM_RSS_URL not configured. Generate a feed at rss.app and set the secret.",
      }),
      { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  const supabase = createClient(SUPABASE_URL, SERVICE_KEY);

  try {
    const res = await fetch(RSS_URL, {
      headers: { "User-Agent": "BasicFoodInstagramSync/1.0" },
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      console.error("RSS fetch failed:", res.status, body.slice(0, 500));
      return new Response(
        JSON.stringify({ error: "RSS fetch failed", status: res.status }),
        { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const xml = await res.text();
    const parsed = parseFeed(xml);

    if (parsed.length === 0) {
      return new Response(
        JSON.stringify({ ok: true, synced: 0, message: "No items in feed" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const rows = parsed.map((n) => ({
      ig_id: n.ig_id,
      media_type: n.media_type,
      media_url: n.media_url,
      thumbnail_url: n.thumbnail_url,
      permalink: n.permalink,
      caption: n.caption,
      timestamp: n.timestamp,
      like_count: 0,
      comments_count: 0,
      raw_payload: n as unknown as Record<string, unknown>,
    }));

    const { error } = await supabase
      .from("instagram_posts")
      .upsert(rows, { onConflict: "ig_id" });

    if (error) {
      console.error("DB upsert error:", error);
      return new Response(
        JSON.stringify({ error: error.message }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    return new Response(
      JSON.stringify({ ok: true, synced: rows.length, source: "rss" }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : "unknown error";
    console.error("instagram-sync exception:", msg);
    return new Response(
      JSON.stringify({ error: msg }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
