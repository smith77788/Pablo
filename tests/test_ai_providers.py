from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from services.ai_providers import configured_providers


def test_configured_providers_returns_only_configured_keys(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    assert configured_providers() == []


def test_configured_providers_honors_order_and_deduplicates_models(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("AI_PROVIDER_ORDER", "groq,openrouter")
    monkeypatch.setenv("GROQ_MODELS", "llama-a,llama-a,llama-b")
    monkeypatch.setenv("OPENROUTER_MODELS", "free-a,free-a,free-b")

    providers = configured_providers()

    assert [provider.name for provider in providers] == ["groq", "openrouter"]
    assert providers[0].models == ["llama-a", "llama-b"]
    assert providers[1].models == ["free-a", "free-b"]


def test_blank_model_env_falls_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_MODELS", "")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    providers = configured_providers()

    assert providers[0].name == "openrouter"
    assert providers[0].models
    assert providers[0].models[0] == "google/gemini-2.0-flash-exp:free"
