"""Provider selection for BotMother AI.

OpenRouter, Groq and Gemini expose OpenAI-compatible chat completions APIs, so
the assistant can keep one tool-calling path while rotating providers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
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

    groq_key = os.getenv("GROQ_API_KEY", "")
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

    gemini_key = os.getenv("GEMINI_API_KEY", "")
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

    order = _csv_env("AI_PROVIDER_ORDER", "openrouter,groq,gemini")
    ordered = [providers[name] for name in order if name in providers]
    ordered.extend(
        provider for name, provider in providers.items() if provider not in ordered
    )
    return ordered
