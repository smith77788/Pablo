"""Перебор ИИ-моделей переживает снятые бесплатные модели OpenRouter.

Виртуальный администратор падал с «ни один AI-провайдер не ответил: 404 No
endpoints found for mistralai/mistral-7b-instruct:free»: все зашитые бесплатные
модели OpenRouter были сняты, а сообщение показывало только последнюю ошибку.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from services import ai_claude, spintax_ai
from services.ai_providers import AiProvider
from services.spintax_service import SpintaxServiceError


class _Err(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code = status


def _fake_openai(monkeypatch, *, live, catalog, calls, cut=()):
    class _Completions:
        async def create(self, *, model, **kw):
            calls.append(model)
            if model in cut:
                msg = types.SimpleNamespace(content="Але давайте подивимось на реальність")
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=msg, finish_reason="length")])
            if model in live:
                msg = types.SimpleNamespace(content="готовый пост")
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=msg, finish_reason="stop")])
            raise _Err(404, f"Error code: 404 - No endpoints found for {model}.")

    class _Models:
        async def list(self):
            return types.SimpleNamespace(data=[types.SimpleNamespace(id=i) for i in catalog])

    class AsyncOpenAI:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=_Completions())
            self.models = _Models()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=AsyncOpenAI))
    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setenv("SPIN_MODELS", "dead/a:free")
    prov = AiProvider(name="openrouter", api_key="k", base_url="https://x",
                      models=["dead/b:free", "mistralai/mistral-7b-instruct:free"])
    monkeypatch.setattr(spintax_ai, "configured_providers", lambda: [prov])


def test_dead_models_fall_through_to_live_catalog(monkeypatch):
    calls: list = []
    _fake_openai(monkeypatch, live={"qwen/qwen3:free"},
                 catalog=["openai/gpt-4o", "qwen/qwen3:free", "dead/b:free"], calls=calls)
    assert asyncio.run(spintax_ai.complete("s", "u")) == "готовый пост"
    assert calls[-1] == "qwen/qwen3:free"
    assert calls.count("dead/b:free") == 1, "снятую модель пробовали повторно"
    assert "openai/gpt-4o" not in calls, "платную модель взяли без спроса"


def test_all_failed_message_is_human(monkeypatch):
    calls: list = []
    _fake_openai(monkeypatch, live=set(), catalog=[], calls=calls)
    with pytest.raises(SpintaxServiceError) as e:
        asyncio.run(spintax_ai.complete("s", "u"))
    msg = str(e.value)
    assert "перепробовано моделей: 3" in msg and "модель снята у провайдера" in msg
    assert "No endpoints" not in msg


@pytest.mark.parametrize("status,text,needle", [
    (401, "bad", "ключ не принят"), (402, "x", "нет средств"),
    (429, "x", "лимит"), (503, "x", "сбой на стороне"),
])
def test_explain_error(status, text, needle):
    assert needle in spintax_ai.explain_error(_Err(status, text))


def test_dead_mistral_not_in_defaults(monkeypatch):
    from services import ai_providers
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)
    prov = [p for p in ai_providers.configured_providers() if p.name == "openrouter"][0]
    assert "mistralai/mistral-7b-instruct:free" not in prov.models


def test_cut_answer_is_not_returned_next_model_is_tried(monkeypatch):
    """Пост в канале обрывался на полуслове: ответ, упёршийся в лимит, принимался."""
    calls: list = []
    _fake_openai(monkeypatch, live={"dead/b:free"}, catalog=[], calls=calls, cut={"dead/a:free"})
    assert asyncio.run(spintax_ai.complete("s", "u")) == "готовый пост"
    assert calls[:2] == ["dead/a:free", "dead/b:free"]


def test_only_cut_answers_give_human_error(monkeypatch):
    calls: list = []
    _fake_openai(monkeypatch, live=set(), catalog=[], calls=calls,
                 cut={"dead/a:free", "dead/b:free", "mistralai/mistral-7b-instruct:free"})
    with pytest.raises(SpintaxServiceError) as e:
        asyncio.run(spintax_ai.complete("s", "u"))
    assert "оборван" in str(e.value)


def test_thinking_models_go_last_in_live_catalog():
    class _Models:
        async def list(self):
            return types.SimpleNamespace(data=[types.SimpleNamespace(id=i) for i in (
                "deepseek/deepseek-r1:free", "qwen/qwq-32b:free", "meta-llama/llama-3.3-70b:free")])

    client = types.SimpleNamespace(models=_Models())
    got = asyncio.run(spintax_ai._live_free_models(client))
    assert got[0] == "meta-llama/llama-3.3-70b:free"
