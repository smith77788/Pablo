/**
 * Outreach Action Executor — викликається з tribunal-enforcer
 * після позитивного вердикту.
 *
 * Stage 1: для Reddit постимо лише якщо є OAuth-ключі
 * (REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD) ТА reddit_posting_enabled=true в settings.
 * Без ключів — позначаємо action як "approved" (готовий, але не опублікований)
 * і повертаємо ok:true (щоб enforcer не помітив помилку).
 *
 * Інші канали (blog/google/telegram/instagram) у Stage 1 — теж "approved".
 */
import { corsHeaders, svcClient, checkDailyRateLimit, getSettings, type OutreachChannel } from "../_shared/outreach.ts";
import { requireInternalCaller } from "../_shared/auth.ts";

const REDDIT = {
  client_id: Deno.env.get("REDDIT_CLIENT_ID"),
  client_secret: Deno.env.get("REDDIT_CLIENT_SECRET"),
  username: Deno.env.get("REDDIT_USERNAME"),
  password: Deno.env.get("REDDIT_PASSWORD"),
  user_agent: Deno.env.get("REDDIT_USER_AGENT") ?? "basic-food-bot/0.1",
};
const GATEWAY_URL = "https://connector-gateway.lovable.dev/telegram";
const LOVABLE_API_KEY = Deno.env.get("LOVABLE_API_KEY");
const TELEGRAM_API_KEY = Deno.env.get("TELEGRAM_API_KEY") ?? Deno.env.get("TELEGRAM_API_KEY_1");

let _redditToken: { access: string; exp: number } | null = null;
async function redditOauthToken(): Promise<string | null> {
  if (!REDDIT.client_id || !REDDIT.client_secret || !REDDIT.username || !REDDIT.password) return null;
  if (_redditToken && Date.now() < _redditToken.exp - 30_000) return _redditToken.access;
  const basic = btoa(`${REDDIT.client_id}:${REDDIT.client_secret}`);
  const body = new URLSearchParams({
    grant_type: "password",
    username: REDDIT.username,
    password: REDDIT.password,
  });
  const res = await fetch("https://www.reddit.com/api/v1/access_token", {
    method: "POST",
    headers: {
      Authorization: `Basic ${basic}`,
      "User-Agent": REDDIT.user_agent,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });
  if (!res.ok) {
    console.warn("[executor] reddit token:", res.status, (await res.text()).slice(0, 200));
    return null;
  }
  const j = await res.json();
  _redditToken = { access: j.access_token, exp: Date.now() + (j.expires_in ?? 3600) * 1000 };
  return _redditToken.access;
}

async function redditPostComment(parentFullname: string, text: string): Promise<{
  ok: boolean; url?: string; error?: string;
}> {
  const token = await redditOauthToken();
  if (!token) return { ok: false, error: "no_credentials" };
  const body = new URLSearchParams({ api_type: "json", thing_id: parentFullname, text });
  const res = await fetch("https://oauth.reddit.com/api/comment", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": REDDIT.user_agent,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) return { ok: false, error: `${res.status}: ${JSON.stringify(j).slice(0, 200)}` };
  const errors = j?.json?.errors ?? [];
  if (errors.length) return { ok: false, error: JSON.stringify(errors) };
  const permalink = j?.json?.data?.things?.[0]?.data?.permalink;
  return { ok: true, url: permalink ? `https://www.reddit.com${permalink}` : undefined };
}

async function telegramSendMessage(chatId: number, text: string): Promise<{ ok: boolean; error?: string }> {
  if (!LOVABLE_API_KEY) return { ok: false, error: "missing_lovable_api_key" };
  if (!TELEGRAM_API_KEY) return { ok: false, error: "missing_telegram_api_key" };

  const res = await fetch(`${GATEWAY_URL}/sendMessage`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${LOVABLE_API_KEY}`,
      "X-Connection-Api-Key": TELEGRAM_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: false,
    }),
  });

  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    return { ok: false, error: `telegram_send_failed_${res.status}: ${JSON.stringify(payload).slice(0, 300)}` };
  }
  return { ok: true };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;
  try {
    const sb = svcClient();
    const body = await req.json().catch(() => ({} as any));
    // tribunal-enforcer передає `from_tribunal=true`, `proposed_change`, `conditions`
    const proposed = body?.proposed_change ?? {};
    const conditions = body?.conditions ?? {};
    const action_id: string | undefined = proposed?.action_id;

    if (!action_id) {
      return new Response(JSON.stringify({ ok: false, error: "no_action_id", message: "Не передано ідентифікатор дії, тому система не зрозуміла, що саме треба виконати." }), {
        status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const { data: action, error: aErr } = await sb
      .from("outreach_actions")
      .select("id, channel, action_type, draft_text, draft_alt_text, status, lead_id, promo_code")
      .eq("id", action_id)
      .single();
    if (aErr || !action) {
      return new Response(JSON.stringify({ ok: false, error: "action_not_found", message: "Не знайдено підготовлену дію для відправки. Можливо, її вже видалено або вона ще не створена." }), {
        status: 404, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const { data: lead } = await sb.from("outreach_leads")
      .select("source_url, source_platform_id, channel, raw_payload")
      .eq("id", action.lead_id).single();

    // Створюємо промокод у БД (якщо ще немає) — щоб ROI міг JOIN'ити по orders.promo_code_id
    if (action.promo_code) {
      const { data: existing } = await sb
        .from("promo_codes").select("id").eq("code", action.promo_code).maybeSingle();
      if (!existing) {
        await sb.from("promo_codes").insert({
          code: action.promo_code,
          discount_type: "percent",
          discount_value: 10,
          max_uses: 50,
          is_active: true,
          starts_at: new Date().toISOString(),
          ends_at: new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString(),
        });
      }
    }

    const settings = await getSettings();
    const channel = action.channel as OutreachChannel;

    // Rate limit (за каналом)
    const limit = settings.rate_limits?.[channel] ?? 5;
    const rl = await checkDailyRateLimit(channel, limit);
    if (!rl.allowed) {
      await sb.from("outreach_actions").update({
        status: "skipped",
        failed_reason: `rate_limit_${channel}: used ${rl.used}/${rl.limit}`,
        retry_count: 0,
      }).eq("id", action_id);
      return new Response(JSON.stringify({
        ok: true, // enforcer вважає це успіхом — дія пропущена свідомо
        action: "skipped_rate_limit", used: rl.used, limit: rl.limit,
      }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
    }

    const finalText = (conditions?.use_alt ? action.draft_alt_text : action.draft_text) ?? action.draft_text;

    // === Reddit posting ===
    if (channel === "reddit" && settings.reddit_posting_enabled) {
      const parent = lead?.source_platform_id ? `t3_${lead.source_platform_id}` : null;
      if (!parent) {
        await sb.from("outreach_actions").update({
          status: "failed", failed_reason: "missing_reddit_parent_id",
        }).eq("id", action_id);
        return new Response(JSON.stringify({ ok: false, error: "missing_reddit_parent_id" }), {
          status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      const r = await redditPostComment(parent, finalText);
      if (r.ok) {
        await sb.from("outreach_actions").update({
          status: "posted", posted_at: new Date().toISOString(), posted_url: r.url ?? null,
        }).eq("id", action_id);
        await sb.from("outreach_leads").update({ status: "acted" }).eq("id", action.lead_id);
        return new Response(JSON.stringify({ ok: true, action: "posted", url: r.url }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      await sb.from("outreach_actions").update({
        status: "failed", failed_reason: `reddit_post: ${r.error}`,
        retry_count: 1,
      }).eq("id", action_id);
      return new Response(JSON.stringify({ ok: false, error: r.error }), {
        status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // === Telegram auto-reply for real customer chats ===
    if (channel === "telegram" && settings.telegram_posting_enabled) {
      const raw = (lead?.raw_payload ?? {}) as Record<string, unknown>;
      const source = String(raw.source ?? "");
      const chatId = Number(raw.chat_id ?? 0);

      if (source === "tg_inbox" && Number.isFinite(chatId) && chatId > 0) {
        const send = await telegramSendMessage(chatId, finalText);
        if (send.ok) {
          await sb.from("outreach_actions").update({
            status: "posted",
            posted_at: new Date().toISOString(),
            posted_url: lead?.source_url ?? null,
            failed_reason: null,
          }).eq("id", action_id);
          await sb.from("outreach_leads").update({ status: "acted" }).eq("id", action.lead_id);
          await sb.from("telegram_messages").insert({
            chat_id: chatId,
            direction: "out",
            message_text: finalText,
            sender_name: "Outreach Hunter",
          });
          return new Response(JSON.stringify({ ok: true, action: "telegram_sent", chat_id: chatId }), {
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }

        await sb.from("outreach_actions").update({
          status: "failed",
          failed_reason: send.error ?? "telegram_send_failed",
          retry_count: 1,
        }).eq("id", action_id);
        return new Response(JSON.stringify({ ok: false, error: send.error ?? "telegram_send_failed" }), {
          status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
    }

    // === Stage 1 fallback: канал не активний для авто-постингу ===
    // Маркуємо як approved (готовий до ручної публікації / чекає ключів)
    await sb.from("outreach_actions").update({
      status: "approved",
      failed_reason: channel === "reddit"
        ? "reddit_posting_disabled_or_no_credentials"
        : channel === "telegram"
          ? "telegram_auto_reply_unavailable_for_this_source"
        : `${channel}_posting_not_enabled_in_stage_1`,
    }).eq("id", action_id);
    await sb.from("outreach_leads").update({ status: "queued" }).eq("id", action.lead_id);

    return new Response(JSON.stringify({
      ok: true,
      action: "draft_ready",
      reason: "Posting disabled — draft saved for manual review or future stage.",
    }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
  } catch (e: any) {
    console.error("[executor] fatal:", e);
    try {
      await svcClient().from("agent_runs").insert({
        function_name: "outreach-action-executor",
        trigger: "tribunal-enforcer",
        status: "error",
        started_at: new Date(Date.now() - 2000).toISOString(),
        finished_at: new Date().toISOString(),
        error_message: String(e?.message ?? e).slice(0, 2000),
      });
    } catch { /* ignore */ }
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
