"""Provider selection for Infragram AI.

OpenRouter, Groq and Gemini expose OpenAI-compatible chat completions APIs, so
the assistant can keep one tool-calling path while rotating providers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Переопределения ключей из БД (platform_settings) имеют приоритет над env.
# Позволяет настраивать AI-ключи из UI (без доступа к переменным окружения) —
# иначе narrative/growth/ai-assistant/SEO-AI не работают без правки env.
_KEY_OVERRIDES: dict[str, str] = {}


def set_ai_keys(mapping: dict[str, str]) -> None:
    """Задать/обновить AI-ключи из БД (env-имена: OPENROUTER_API_KEY и т.п.)."""
    for k, v in (mapping or {}).items():
        if v:
            _KEY_OVERRIDES[k] = v
        else:
            _KEY_OVERRIDES.pop(k, None)


def _key(name: str) -> str:
    """Ключ: сначала override из БД, затем переменная окружения."""
    return (_KEY_OVERRIDES.get(name) or os.getenv(name, "")).strip()


@dataclass(frozen=True)
class AiProvider:
    name: str
    api_key: str
    base_url: str
    models: list[str]


def _csv_env(name: str, default: str) -> list[str]:
    def _parse(raw_value: str) -> list[str]:
        parsed: list[str] = []
        for part in raw_value.split(","):
            value = part.strip()
            if value and value not in parsed:
                parsed.append(value)
        return parsed

    raw = os.getenv(name, default)
    values = _parse(raw)
    if values:
        return values
    return _parse(default)


def _provider(
    *,
    name: str,
    api_key: str,
    base_url: str,
    models: list[str],
) -> AiProvider | None:
    if not api_key or not models:
        return None
    return AiProvider(name=name, api_key=api_key, base_url=base_url, models=models)


def configured_providers() -> list[AiProvider]:
    """Return providers in configured failover order."""
    providers: dict[str, AiProvider] = {}

    openrouter_key = _key("OPENROUTER_API_KEY")
    if openrouter_key:
        provider = _provider(
            name="openrouter",
            api_key=openrouter_key,
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            models=_csv_env(
                "OPENROUTER_MODELS",
                ",".join(
                    [
                        os.getenv(
                            "OPENROUTER_MODEL", "google/gemini-2.0-flash-exp:free"
                        ),
                        "google/gemini-2.0-flash-exp:free",
                        "meta-llama/llama-3.2-3b-instruct:free",
                        "mistralai/mistral-7b-instruct:free",
                    ]
                ),
            ),
        )
        if provider:
            providers["openrouter"] = provider

    groq_key = _key("GROQ_API_KEY")
    if groq_key:
        provider = _provider(
            name="groq",
            api_key=groq_key,
            base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            models=_csv_env(
                "GROQ_MODELS",
                ",".join(
                    [
                        os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
                        "llama-3.3-70b-versatile",
                    ]
                ),
            ),
        )
        if provider:
            providers["groq"] = provider

    gemini_key = _key("GEMINI_API_KEY")
    if gemini_key:
        provider = _provider(
            name="gemini",
            api_key=gemini_key,
            base_url=os.getenv(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            ),
            models=_csv_env(
                "GEMINI_MODELS",
                ",".join(
                    [
                        os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
                        "gemini-1.5-flash",
                    ]
                ),
            ),
        )
        if provider:
            providers["gemini"] = provider

    # Ollama — local/self-hosted LLM, no API key required.
    # Set OLLAMA_BASE_URL to your server, e.g. http://1.2.3.4:11434/v1
    ollama_url = os.getenv("OLLAMA_BASE_URL", "")
    if ollama_url:
        provider = _provider(
            name="ollama",
            api_key="ollama",  # Ollama ignores the key; non-empty to pass validation
            base_url=ollama_url.rstrip("/"),
            models=_csv_env(
                "OLLAMA_MODELS",
                os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            ),
        )
        if provider:
            providers["ollama"] = provider

    default_order = (
        "ollama,openrouter,groq,gemini"
        if "ollama" in providers
        else "openrouter,groq,gemini"
    )
    order = _csv_env("AI_PROVIDER_ORDER", default_order)
    ordered = [providers[name] for name in order if name in providers]
    ordered.extend(
        provider for name, provider in providers.items() if provider not in ordered
    )
    return ordered
