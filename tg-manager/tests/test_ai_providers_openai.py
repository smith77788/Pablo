"""OPENAI_API_KEY обязан давать рабочего провайдера.

Баг: configured_providers() знал openrouter/groq/gemini/ollama, но НЕ plain OpenAI.
У кого ключ задан как OPENAI_API_KEY (основной ключ платформы), sales-персона и
прочие фичи на configured_providers молча падали в fallback — бот отвечал только
«когда не знает ответа», хотя ключ есть.
"""
from __future__ import annotations

import importlib

from services import ai_providers


def test_openai_key_yields_provider(monkeypatch):
    ai_providers.set_ai_keys({})  # сбросить БД-оверрайды
    # изолируем от других ключей окружения
    for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    provs = ai_providers.configured_providers()
    names = [p.name for p in provs]
    assert "openai" in names, names
    o = next(p for p in provs if p.name == "openai")
    assert o.api_key == "sk-test-123" and o.models and o.base_url.endswith("openai.com/v1")


def test_openai_key_via_db_override(monkeypatch):
    for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
              "GEMINI_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    ai_providers.set_ai_keys({"OPENAI_API_KEY": "sk-db-999"})
    try:
        assert any(p.name == "openai" for p in ai_providers.configured_providers())
    finally:
        ai_providers.set_ai_keys({"OPENAI_API_KEY": ""})  # cleanup override
