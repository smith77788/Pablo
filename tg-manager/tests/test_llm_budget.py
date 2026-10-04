"""Лимиты бесплатных моделей ИИ — пауза до сброса, а не долбёжка.

Раньше каждый вызов ИИ перебирал все модели подряд: на исчерпанном лимите каждый
такт каждого канала заново получал 429 от десятка моделей и сжигал остаток
квоты. С сотней каналов под виртуальным администратором посты не получали даже
не все каналы. Теперь модель, упёршаяся в лимит, ставится на паузу до сброса
(services/llm_gate), а виртуальный администратор пишет посты заранее пачками.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from services import ai_claude, channel_admin as ca, llm_gate, spintax_ai
from services.ai_providers import AiProvider


class _Err(Exception):
    def __init__(self, status, msg, headers=None):
        super().__init__(msg)
        self.status_code = status
        self.response = types.SimpleNamespace(headers=headers or {})


# ── Разбор ответа провайдера ────────────────────────────────────────────────


def test_classify_minute_limit_with_retry_after():
    kind, hint = llm_gate.classify_limit(
        _Err(429, "Rate limit exceeded: free-models-per-min", {"Retry-After": "17"}))
    assert kind == "minute" and hint == 17


def test_classify_daily_and_credits_and_plain_error():
    assert llm_gate.classify_limit(
        _Err(429, "Rate limit exceeded: free-models-per-day"))[0] == "daily"
    assert llm_gate.classify_limit(_Err(402, "Insufficient credits"))[0] == "credits"
    assert llm_gate.classify_limit(_Err(500, "internal error")) is None
    assert llm_gate.classify_limit(_Err(404, "No endpoints found")) is None


def test_groq_reset_format():
    _, hint = llm_gate.classify_limit(
        _Err(429, "rate_limit_exceeded", {"x-ratelimit-reset-requests": "2m59.5s"}))
    assert 179 < hint < 180


def test_daily_limit_on_free_model_pauses_all_free_models_not_paid():
    async def go():
        await llm_gate.note_limit("openrouter", "a/x:free",
                                  _Err(429, "Rate limit exceeded: free-models-per-day"))
        assert await llm_gate.blocked_until("openrouter", "b/y:free")
        assert await llm_gate.blocked_until("openrouter", "openai/gpt-4o") is None
    asyncio.run(go())


def test_minute_pause_escalates_and_success_resets():
    async def go():
        exc = _Err(429, "too many requests")
        t1 = await llm_gate.note_limit("groq", "m", exc)
        t2 = await llm_gate.note_limit("groq", "m", exc)
        assert t2 - t1 > timedelta(seconds=15), "повторный удар должен удлинять паузу"
        await llm_gate.note_ok("groq", "m")
        assert await llm_gate.blocked_until("groq", "m") is None
    asyncio.run(go())


# ── Перебор моделей уважает паузы ───────────────────────────────────────────


def _fake_openai(monkeypatch, *, models, behaviour, calls):
    class _Completions:
        async def create(self, *, model, **kw):
            calls.append(model)
            b = behaviour.get(model, "ok")
            if b == "ok":
                msg = types.SimpleNamespace(content="готовый пост")
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=msg, finish_reason="stop")])
            raise _Err(429, b)

    class _Models:
        async def list(self):
            return types.SimpleNamespace(data=[])

    class AsyncOpenAI:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=_Completions())
            self.models = _Models()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=AsyncOpenAI))
    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.delenv("SPIN_MODELS", raising=False)
    prov = AiProvider(name="groq", api_key="k", base_url="https://x", models=list(models))
    monkeypatch.setattr(spintax_ai, "configured_providers", lambda: [prov])
    monkeypatch.setattr(spintax_ai, "models_for", lambda p: list(models))


def test_limited_model_is_not_hit_again(monkeypatch):
    calls: list = []
    _fake_openai(monkeypatch, models=["a", "b"], calls=calls,
                 behaviour={"a": "Rate limit reached for requests per minute"})
    assert asyncio.run(spintax_ai.complete("s", "u")) == "готовый пост"
    assert calls == ["a", "b"]
    calls.clear()
    assert asyncio.run(spintax_ai.complete("s", "u")) == "готовый пост"
    assert calls == ["b"], "модель на паузе снова получила запрос"


def test_all_limited_is_a_pause_not_a_failure(monkeypatch):
    calls: list = []
    _fake_openai(monkeypatch, models=["a", "b"], calls=calls,
                 behaviour={"a": "Rate limit reached for requests per day (RPD)",
                            "b": "Rate limit reached for requests per day (RPD)"})
    with pytest.raises(spintax_ai.AiPaused) as e:
        asyncio.run(spintax_ai.complete("s", "u"))
    assert calls == ["a"], "после суточного лимита провайдера остальные его модели не трогаем"
    assert e.value.retry_at > datetime.now(timezone.utc) + timedelta(minutes=1)
    calls.clear()
    with pytest.raises(spintax_ai.AiPaused):
        asyncio.run(spintax_ai.complete("s", "u"))
    assert calls == [], "на паузе к провайдеру не должно уходить ни одного запроса"


def test_channel_admin_maps_pause_to_ai_busy():
    async def paused(system, user):
        raise spintax_ai.AiPaused(datetime.now(timezone.utc) + timedelta(minutes=3))

    with pytest.raises(ca.AiBusy) as e:
        asyncio.run(ca._ask(paused, "s", "u"))
    assert e.value.retry_at is not None


# ── Пакет постов ────────────────────────────────────────────────────────────


def test_parse_batch_formats():
    assert ca.parse_batch('["один", "два"]', 2) == ["один", "два"]
    assert ca.parse_batch('```json\n{"posts": [{"text": "x"}, "y", "z"]}\n```', 2) == ["x", "y"]
    assert ca.parse_batch("просто текст", 3) == []


def test_batch_prompt_lists_every_task():
    profile = {"topic": "доставка мебели", "audience": "семьи", "title": "Мебель",
               "lead_contact": "@m"}
    system, user = ca.build_post_prompt(
        profile, pillar="Советы", recent_texts=[],
        tasks=[("Советы", "упаковка"), ("Кейсы", ""), ("Предложение недели", "")])
    assert "напиши 3 РАЗНЫХ поста" in user and "JSON-массив" in user
    assert "1. Рубрика «Советы»; тема: упаковка" in user and "3. Рубрика «Предложение недели»" in user


class _RecPool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def test_ai_wait_postpones_without_failure():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    pool = _RecPool()
    admin = {"owner_id": 1, "channel_id": 2, "last_error": None}
    asyncio.run(ca._wait_ai(pool, admin, now + timedelta(hours=9), now))
    upd = pool.calls[0]
    assert "fail_streak" not in upd[0], "лимит ИИ не сбой канала"
    assert upd[1][2] == now + timedelta(hours=1), "перенос не дальше часа"
    assert upd[1][3].startswith("Жду ИИ")
    assert any("va_admin_events" in c[0] for c in pool.calls)
    # Повторное ожидание журнал не засоряет.
    pool2 = _RecPool()
    asyncio.run(ca._wait_ai(pool2, {**admin, "last_error": upd[1][3]}, None, now))
    assert pool2.calls[0][1][2] == now + timedelta(minutes=15)
    assert not any("va_admin_events" in c[0] for c in pool2.calls)


def test_news_channel_is_never_written_ahead(monkeypatch):
    """Новости пишутся по свежим событиям: заранее написанная «новость» — уже не новость."""
    from unittest.mock import AsyncMock

    pool = AsyncMock()
    pool.fetch.return_value = [{"id": 1, "pillar": "Главное", "topic": ""}]
    monkeypatch.setattr(ca, "get_admin", AsyncMock(return_value={"topic": "Оперативные новости"}))
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Свежие новости"}))
    complete = AsyncMock(return_value='["текст"]')
    assert asyncio.run(ca.prewrite(pool, 1, 2, complete=complete)) == 0
    complete.assert_not_awaited()
    sql, *args = pool.execute.call_args.args
    assert "write_attempts" in sql and args[1] == ca._PREWRITE_MAX_ATTEMPTS


def test_prewrite_horizon_is_one_day():
    """Писать впрок на трое суток — тратить лимит на тексты, которые устареют."""
    assert ca._PREWRITE_AHEAD_H <= 24
