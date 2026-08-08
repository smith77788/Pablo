"""OpenAI-compatible chat engine for the assistant bot.

Talks to Claude (and other models) through OpenRouter's OpenAI-compatible
Chat Completions API, reusing the same OPENROUTER_API_KEY that tg-manager
already uses — so the owner needs no separate (paid) Anthropic key.

History is kept in OpenAI message format ({"role": ..., "content": ...}) so it
serialises to JSON and survives bot restarts.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# Matches tg-manager's default (Claude via OpenRouter). Override with
# OPENROUTER_MODEL, or per-chat with /model.
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
MAX_TOKENS = 4096
REQUEST_TIMEOUT = 120.0

SYSTEM_PROMPT = """\
Ты — Pablo, личный ИИ-ассистент в Telegram (бот ClaudeX).

Правила общения:
- Отвечай на языке собеседника (русский или украинский).
- Ответ уходит в Telegram обычным текстом: НЕ используй markdown-разметку
  (##, **, таблицы, ```). Структурируй списками с дефисами и эмодзи.
- Будь краток и по делу: сначала итог, потом детали.
- Если чего-то не знаешь — скажи честно.
"""


class NoProviderError(RuntimeError):
    """No AI provider key is configured."""


class AIError(RuntimeError):
    """The provider returned an error."""


def _api_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or "").strip()


def _base_url() -> str:
    return (os.getenv("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def default_model() -> str:
    return (os.getenv("OPENROUTER_MODEL") or "").strip() or DEFAULT_MODEL


def provider_ready() -> bool:
    return bool(_api_key())


class AIChat:
    """One chat turn against an OpenAI-compatible endpoint over a JSON history."""

    def run_turn(
        self,
        model: str,
        history: list[dict],
        user_content,
        on_progress=None,
    ) -> tuple[str, list[dict]]:
        """Append the user message, call the model, return (reply, history)."""
        key = _api_key()
        if not key:
            raise NoProviderError(
                "OPENROUTER_API_KEY не задан — добавь ключ OpenRouter в переменные "
                "окружения (тот же, что использует tg-manager)."
            )

        history = list(history)
        history.append({"role": "user", "content": user_content})

        if on_progress:
            on_progress()

        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        payload = {
            "model": model or default_model(),
            "max_tokens": MAX_TOKENS,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional but recommended).
            "HTTP-Referer": "https://github.com/smith77788/Pablo",
            "X-Title": "Pablo Assistant",
        }

        try:
            resp = httpx.post(
                f"{_base_url()}/chat/completions",
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise AIError(f"сеть недоступна: {e}") from e

        if resp.status_code == 401:
            raise NoProviderError("провайдер отклонил ключ (401 Unauthorized).")
        if resp.status_code >= 400:
            raise AIError(f"{resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        text = _extract_text(data)
        history.append({"role": "assistant", "content": text})
        return text or "(пустой ответ)", history


def _extract_text(data: dict) -> str:
    try:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content")
    except (KeyError, IndexError, TypeError):
        raise AIError(f"неожиданный ответ провайдера: {str(data)[:300]}")

    if isinstance(content, str):
        return content.strip()
    # Some providers return content as a list of parts.
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ]
        return "\n".join(parts).strip()
    return ""
