/**
 * debug-report — public ingestion endpoint for client-side errors.
 *
 * Called by:
 *   - src/lib/debug.ts (web + Capacitor APK)
 *   - any external integration that wants to log into our debug pipeline
 *
 * Public on purpose (no JWT) — the RLS policy on debug_reports plus the
 * server-side validation in `ingest_debug_report` constrain abuse.
 */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { rateLimit, getClientIp, rateLimitResponse } from "../_shared/rate-limit.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const ALLOWED_PLATFORMS = new Set([
  "web", "android", "native_ios", "telegram_bot", "edge_function", "cron", "unknown",
]);
const ALLOWED_LEVELS = new Set(["info", "warn", "error", "fatal"]);

async function fingerprintFor(platform: string, source: string, message: string): Promise<string> {
  const firstLine = (message || "").split("\n")[0].slice(0, 200);
  const raw = `${platform}|${source}|${firstLine}`;
  const buf = new TextEncoder().encode(raw);
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(hash))
    .slice(0, 16)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "method_not_allowed" }), {
      status: 405,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const rl = rateLimit(`debug-report:${getClientIp(req)}`, { capacity: 10, refillPerSec: 0.2 });
  if (!rl.ok) return rateLimitResponse(rl, corsHeaders);

  try {
    const body = await req.json();
    const platform = ALLOWED_PLATFORMS.has(body.platform) ? body.platform : "unknown";
    const level = ALLOWED_LEVELS.has(body.level) ? body.level : "error";
    const source = String(body.source ?? "unknown").slice(0, 200);
    const message = String(body.message ?? "").slice(0, 4000);
    const stack = body.stack ? String(body.stack).slice(0, 16000) : null;
    const context = body.context && typeof body.context === "object" ? body.context : {};

    if (!message) {
      return new Response(JSON.stringify({ error: "message_required" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const fp = body.fingerprint
      ? String(body.fingerprint).slice(0, 128)
      : await fingerprintFor(platform, source, message);

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
      { auth: { persistSession: false } },
    );

    const { data, error } = await (supabase as any).rpc("ingest_debug_report", {
      p_platform: platform,
      p_level: level,
      p_source: source,
      p_message: message,
      p_stack: stack,
      p_context: context,
      p_fingerprint: fp,
    } as any);

    if (error) {
      return new Response(JSON.stringify({ error: error.message }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ ok: true, id: data, fingerprint: fp }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "unknown";
    return new Response(JSON.stringify({ error: msg }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
