/**
 * AI Router — multi-provider rotation with automatic fallback.
 * 
 * Providers (priority order):
 * 1. Google AI Studio (Gemini 2.0 Flash) — 1M tokens/day free
 * 2. Groq (llama-3.3-70b) — 14,400 req/day free
 * 3. OpenRouter (free models) — ~200 req/day free
 * 4. Lovable AI Gateway — paid fallback
 * 
 * All providers use OpenAI-compatible API format.
 */

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { sanitizePlaceholders } from "./sanitize-placeholders.ts";

// ── Provider configs ──

interface ProviderConfig {
  name: string;
  baseUrl: string;
  getHeaders: () => Record<string, string>;
  defaultModel: string;
  /** Map from generic model aliases to provider-specific model names */
  modelMap: Record<string, string>;
  supportsVision?: boolean;
  available: () => boolean;
}

const PROVIDERS: ProviderConfig[] = [
  // Quality-priority order: Together (Llama 3.3 70B) → Cohere (Command R+) → Google AI → Groq → OpenRouter → Lovable
  {
    name: "together",
    baseUrl: "https://api.together.xyz/v1/chat/completions",
    getHeaders: () => ({
      Authorization: `Bearer ${Deno.env.get("TOGETHER_API_KEY")}`,
      "Content-Type": "application/json",
    }),
    defaultModel: "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    modelMap: {
      "google/gemini-2.5-pro": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
      "google/gemini-2.5-flash": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
      "google/gemini-2.5-flash-lite": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
      "google/gemini-3-flash-preview": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    },
    supportsVision: false,
    available: () => !!Deno.env.get("TOGETHER_API_KEY"),
  },
  {
    name: "cohere",
    // Cohere has its own API — using their OpenAI-compat endpoint
    baseUrl: "https://api.cohere.ai/compatibility/v1/chat/completions",
    getHeaders: () => ({
      Authorization: `Bearer ${Deno.env.get("COHERE_API_KEY")}`,
      "Content-Type": "application/json",
    }),
    defaultModel: "command-r-plus-08-2024",
    modelMap: {
      "google/gemini-2.5-pro": "command-r-plus-08-2024",
      "google/gemini-2.5-flash": "command-r-08-2024",
      "google/gemini-2.5-flash-lite": "command-r7b-12-2024",
      "google/gemini-3-flash-preview": "command-r-plus-08-2024",
    },
    supportsVision: false,
    available: () => !!Deno.env.get("COHERE_API_KEY"),
  },
  {
    name: "google_ai",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    getHeaders: () => ({
      Authorization: `Bearer ${Deno.env.get("GEMINI_API_KEY")}`,
      "Content-Type": "application/json",
    }),
    defaultModel: "gemini-2.0-flash",
    modelMap: {
      "google/gemini-2.5-pro": "gemini-2.0-flash",
      "google/gemini-2.5-flash": "gemini-2.0-flash",
      "google/gemini-2.5-flash-lite": "gemini-2.0-flash",
      "google/gemini-3-flash-preview": "gemini-2.0-flash",
    },
    supportsVision: true,
    available: () => !!Deno.env.get("GEMINI_API_KEY"),
  },
  {
    name: "groq",
    baseUrl: "https://api.groq.com/openai/v1/chat/completions",
    getHeaders: () => ({
      Authorization: `Bearer ${Deno.env.get("GROQ_API_KEY")}`,
      "Content-Type": "application/json",
    }),
    defaultModel: "llama-3.3-70b-versatile",
    modelMap: {
      "google/gemini-2.5-pro": "llama-3.3-70b-versatile",
      "google/gemini-2.5-flash": "llama-3.3-70b-versatile",
      "google/gemini-2.5-flash-lite": "llama-3.1-8b-instant",
      "google/gemini-3-flash-preview": "llama-3.3-70b-versatile",
    },
    supportsVision: false,
    available: () => !!Deno.env.get("GROQ_API_KEY"),
  },
  {
    name: "openrouter",
    baseUrl: "https://openrouter.ai/api/v1/chat/completions",
    getHeaders: () => ({
      Authorization: `Bearer ${Deno.env.get("OPENROUTER_API_KEY")}`,
      "Content-Type": "application/json",
      "HTTP-Referer": "https://basic-food.shop",
      "X-Title": "BASIC.FOOD ACOS",
    }),
    defaultModel: "google/gemini-2.0-flash-exp:free",
    modelMap: {
      "google/gemini-2.5-pro": "google/gemini-2.0-flash-exp:free",
      "google/gemini-2.5-flash": "google/gemini-2.0-flash-exp:free",
      "google/gemini-2.5-flash-lite": "deepseek/deepseek-chat-v3-0324:free",
      "google/gemini-3-flash-preview": "google/gemini-2.0-flash-exp:free",
    },
    supportsVision: true,
    available: () => !!Deno.env.get("OPENROUTER_API_KEY"),
  },
  {
    name: "lovable",
    baseUrl: "https://ai.gateway.lovable.dev/v1/chat/completions",
    getHeaders: () => ({
      Authorization: `Bearer ${Deno.env.get("LOVABLE_API_KEY")}`,
      "Content-Type": "application/json",
    }),
    defaultModel: "google/gemini-2.5-flash",
    modelMap: {},
    supportsVision: true,
    available: () => !!Deno.env.get("LOVABLE_API_KEY"),
  },
];

// ── Types ──

export interface AIRouterRequest {
  messages: Array<{ role: string; content: string | unknown[] }>;
  model?: string;
  tools?: any[];
  tool_choice?: any;
  response_format?: any;
  temperature?: number;
  max_tokens?: number;
  /** If true, skip cache lookup */
  noCache?: boolean;
  /** Cache TTL in seconds (default 7 days) */
  cacheTtlSeconds?: number;
  /**
   * Lovable AI Gateway usage. DEFAULT: true (skip Lovable, use only independent providers).
   * Set to `false` ONLY if you explicitly want to allow paid Lovable fallback.
   */
  skipLovable?: boolean;
  /** Per-provider HTTP timeout (ms). Default 20s. */
  timeoutMs?: number;
  /** If true, use only providers that can accept multimodal image inputs. */
  requiresVision?: boolean;
}

export interface AIRouterResponse {
  content: string | null;
  toolCalls: any[] | null;
  provider: string;
  model: string;
  cached: boolean;
  tokensUsed?: number;
}

// ── Helpers ──

function getServiceClient() {
  return createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );
}

async function sha256(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// ── Cache ──

async function checkCache(
  promptHash: string,
  systemHash: string | null,
): Promise<AIRouterResponse | null> {
  try {
    const sb = getServiceClient();
    const { data } = await sb
      .from("ai_response_cache")
      .select("response_text, tool_calls, provider, model, tokens_used")
      .eq("prompt_hash", promptHash)
      .eq("system_hash", systemHash ?? "")
      .gt("expires_at", new Date().toISOString())
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    if (!data) return null;
    return {
      content: data.response_text,
      toolCalls: data.tool_calls as any[] | null,
      provider: data.provider,
      model: data.model,
      cached: true,
      tokensUsed: data.tokens_used ?? undefined,
    };
  } catch {
    return null;
  }
}

async function writeCache(
  promptHash: string,
  systemHash: string | null,
  result: AIRouterResponse,
  ttlSeconds: number,
) {
  try {
    const sb = getServiceClient();
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
    await sb.from("ai_response_cache").insert({
      prompt_hash: promptHash,
      system_hash: systemHash ?? "",
      provider: result.provider,
      model: result.model,
      response_text: result.content ?? "",
      tool_calls: result.toolCalls ?? null,
      tokens_used: result.tokensUsed ?? null,
      expires_at: expiresAt,
    });
  } catch {
    // Non-critical
  }
}

// ── Usage tracking ──

async function getAvailableProviders(): Promise<
  Array<{ provider: string; model: string; priority: number }>
> {
  try {
    const sb = getServiceClient();
    // Reset daily counters if needed
    await sb
      .from("ai_provider_usage")
      .update({ tokens_used_today: 0, requests_today: 0, last_reset_at: new Date().toISOString() })
      .lt("last_reset_at", new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString());

    const { data } = await sb
      .from("ai_provider_usage")
      .select("provider, model, priority, tokens_used_today, requests_today, daily_token_limit, daily_request_limit, is_healthy")
      .eq("is_healthy", true)
      .order("priority", { ascending: true });

    return (data ?? [])
      .filter(
        (p) =>
          p.tokens_used_today < p.daily_token_limit &&
          p.requests_today < p.daily_request_limit,
      )
      .map((p) => ({ provider: p.provider, model: p.model, priority: p.priority }));
  } catch {
    // Fallback: try all providers
    return PROVIDERS.filter((p) => p.available()).map((p, i) => ({
      provider: p.name,
      model: p.defaultModel,
      priority: i,
    }));
  }
}

async function recordUsage(provider: string, model: string, tokensUsed: number, error?: string) {
  try {
    const sb = getServiceClient();
    if (error) {
      await sb
        .from("ai_provider_usage")
        .update({
          last_error: error.slice(0, 500),
          last_error_at: new Date().toISOString(),
          is_healthy: false,
        })
        .eq("provider", provider)
        .eq("model", model);
      // Auto-heal after 10 minutes
      setTimeout(async () => {
        try {
          await sb
            .from("ai_provider_usage")
            .update({ is_healthy: true })
            .eq("provider", provider)
            .eq("model", model);
        } catch { /* */ }
      }, 10 * 60 * 1000);
    } else {
      // Use raw SQL via rpc is not available, so do two-step
      const { data: current } = await sb
        .from("ai_provider_usage")
        .select("tokens_used_today, requests_today")
        .eq("provider", provider)
        .eq("model", model)
        .maybeSingle();

      if (current) {
        await sb
          .from("ai_provider_usage")
          .update({
            tokens_used_today: (current.tokens_used_today ?? 0) + tokensUsed,
            requests_today: (current.requests_today ?? 0) + 1,
          })
          .eq("provider", provider)
          .eq("model", model);
      }
    }
  } catch {
    // Non-critical
  }
}

// ── Main call ──

async function callProvider(
  providerConfig: ProviderConfig,
  req: AIRouterRequest,
): Promise<AIRouterResponse> {
  const resolvedModel =
    providerConfig.modelMap[req.model ?? ""] ?? providerConfig.defaultModel;

  const body: any = {
    model: resolvedModel,
    messages: req.messages,
  };

  if (req.temperature !== undefined) body.temperature = req.temperature;
  if (req.max_tokens !== undefined) body.max_tokens = req.max_tokens;

  // Tool calling support varies by provider
  if (req.tools && providerConfig.name !== "groq") {
    body.tools = req.tools;
    if (req.tool_choice) body.tool_choice = req.tool_choice;
  }

  // response_format: json_object support
  if (req.response_format && providerConfig.name !== "groq") {
    body.response_format = req.response_format;
  }

  // For Groq without tool support — add instruction to return JSON
  if (req.tools && providerConfig.name === "groq") {
    const lastMsg = body.messages[body.messages.length - 1];
    if (lastMsg) {
      lastMsg.content += "\n\nIMPORTANT: Return your response as valid JSON only.";
    }
    body.response_format = { type: "json_object" };
  }

  // Per-provider timeout — without this, a slow/hung provider blocks the whole
  // chain and causes the edge function to hit Supabase's 60s wall, which the
  // client sees as a generic "AI недоступний" with no explanation.
  const ctrl = new AbortController();
  const timeoutMs = req.timeoutMs ?? 20_000;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(providerConfig.baseUrl, {
      method: "POST",
      headers: providerConfig.getHeaders(),
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e: any) {
    if (e?.name === "AbortError") {
      throw new Error(`${providerConfig.name} timeout after ${timeoutMs}ms`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }

  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`${providerConfig.name} ${resp.status}: ${errText.slice(0, 300)}`);
  }

  const data = await resp.json();
  const choice = data?.choices?.[0];
  const message = choice?.message;

  const tokensUsed =
    data?.usage?.total_tokens ??
    ((data?.usage?.prompt_tokens ?? 0) + (data?.usage?.completion_tokens ?? 0));

  // Handle tool calls
  if (message?.tool_calls?.length) {
    return {
      content: message.content ?? null,
      toolCalls: message.tool_calls,
      provider: providerConfig.name,
      model: resolvedModel,
      cached: false,
      tokensUsed,
    };
  }

  // For Groq fallback: if we converted tool call to JSON, wrap it back
  if (req.tools && providerConfig.name === "groq" && message?.content) {
    try {
      const parsed = JSON.parse(message.content);
      const toolName = req.tools[0]?.function?.name ?? "result";
      return {
        content: null,
        toolCalls: [
          {
            id: `call_${Date.now()}`,
            type: "function",
            function: { name: toolName, arguments: JSON.stringify(parsed) },
          },
        ],
        provider: providerConfig.name,
        model: resolvedModel,
        cached: false,
        tokensUsed,
      };
    } catch {
      // Not valid JSON, return as content
    }
  }

  return {
    content: message?.content ?? null,
    toolCalls: null,
    provider: providerConfig.name,
    model: resolvedModel,
    cached: false,
    tokensUsed,
  };
}

// ── Public API ──

/**
 * Route an AI request through available providers with automatic fallback.
 * 
 * Usage:
 * ```ts
 * import { routeAI } from "../_shared/ai-router.ts";
 * const result = await routeAI({
 *   messages: [{ role: "user", content: "Hello" }],
 * });
 * console.log(result.content, result.provider);
 * ```
 */
export async function routeAI(req: AIRouterRequest): Promise<AIRouterResponse> {
  // 1. Check cache
  if (!req.noCache) {
    const systemMsg = req.messages.find((m) => m.role === "system")?.content ?? "";
    const userMsg = req.messages
      .filter((m) => m.role === "user")
      .map((m) => (typeof m.content === "string" ? m.content : JSON.stringify(m.content)))
      .join("\n");
    const promptHash = await sha256(userMsg);
    const systemHash = systemMsg
      ? await sha256(typeof systemMsg === "string" ? systemMsg : JSON.stringify(systemMsg))
      : null;

    const cached = await checkCache(promptHash, systemHash);
    if (cached) return cached;

    // 2. Get available providers sorted by priority
    // Default: agents are independent — Lovable AI Gateway is OFF unless explicitly opted-in
    const skipLovable = req.skipLovable !== false;
    const available = await getAvailableProviders();
    const providerOrder = PROVIDERS.filter((p) => {
      if (!p.available()) return false;
      if (skipLovable && p.name === "lovable") return false;
      if (req.requiresVision && !p.supportsVision) return false;
      return available.some((a) => a.provider === p.name);
    });

    // 3. Try each provider
    const errors: string[] = [];
    for (const provider of providerOrder) {
      try {
        const result = await callProvider(provider, req);
        // Record success
        await recordUsage(provider.name, result.model, result.tokensUsed ?? 0);
        // Cache result
        await writeCache(promptHash, systemHash, result, req.cacheTtlSeconds ?? 7 * 24 * 3600);
        return result;
      } catch (e: any) {
        const errMsg = e?.message ?? String(e);
        errors.push(`${provider.name}: ${errMsg}`);
        console.warn(`AI Router: ${provider.name} failed:`, errMsg);
        await recordUsage(
          provider.name,
          provider.modelMap[req.model ?? ""] ?? provider.defaultModel,
          0,
          errMsg,
        );
      }
    }

    throw new Error(`All AI providers failed:\n${errors.join("\n")}`);
  }

  // No cache path — same default: skip Lovable unless explicitly enabled
  const skipLovableNc = req.skipLovable !== false;
  const providerOrder = PROVIDERS.filter((p) => {
    if (!p.available()) return false;
    if (skipLovableNc && p.name === "lovable") return false;
    if (req.requiresVision && !p.supportsVision) return false;
    return true;
  });

  const errors: string[] = [];
  for (const provider of providerOrder) {
    try {
      const result = await callProvider(provider, req);
      await recordUsage(provider.name, result.model, result.tokensUsed ?? 0);
      return result;
    } catch (e: any) {
      const errMsg = e?.message ?? String(e);
      errors.push(`${provider.name}: ${errMsg}`);
      console.warn(`AI Router: ${provider.name} failed:`, errMsg);
      await recordUsage(
        provider.name,
        provider.modelMap[req.model ?? ""] ?? provider.defaultModel,
        0,
        errMsg,
      );
    }
  }

  throw new Error(`All AI providers failed:\n${errors.join("\n")}`);
}

/**
 * Convenience: call AI and get just the text content.
 * Throws if all providers fail.
 *
 * За замовчуванням текст пропускається через sanitizePlaceholders() — це
 * гарантує що outbound (Telegram / email / web push / broadcast) НЕ отримає
 * сирих [ім'я тварини] / {pet_name} / [клієнт] / [порода] плейсхолдерів,
 * навіть якщо LLM проігнорувала промпт.
 *
 * Передайте `petName` коли воно відоме — буде підставлено замість загального
 * "вашого улюбленця". Передайте `skipSanitize: true` для технічних викликів
 * (JSON / tool calls / код), де чищення може зіпсувати корисний вивід.
 */
export async function routeAIText(
  req: AIRouterRequest & { petName?: string | null; skipSanitize?: boolean },
): Promise<string> {
  const result = await routeAI(req);
  let text = "";
  if (result.toolCalls?.[0]?.function?.arguments) {
    text = result.toolCalls[0].function.arguments;
  } else {
    text = result.content ?? "";
  }
  if (req.skipSanitize) return text;
  return sanitizePlaceholders(text, req.petName ?? null);
}

