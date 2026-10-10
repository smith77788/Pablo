"""ИИ-иллюстрации вирт.администратора: решение «по смыслу» + генерация + доставка.

Владелец: «если к посту по смыслу нужно изображение — добавляем, если нет —
пропускаем», провайдеры — «те, что уже заданы у нас». Картинку в очередь не
кладём: в операцию уходит лёгкий media_prompt, op_worker генерирует байты.
Fail-open: нет провайдера/ключа или сбой — пост уходит текстом.
"""
from __future__ import annotations

import inspect

import pytest

from services import va_image


# ── decide_illustration ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decide_yes_returns_prompt(monkeypatch):
    async def fake_complete(system, user, *, timeout=120.0):
        return '{"need": true, "prompt": "a cozy coffee shop, warm light"}'
    monkeypatch.setattr("services.ai_claude.complete", fake_complete)
    need, prompt = await va_image.decide_illustration("Большой анонс нового меню кофейни", "анонс")
    assert need is True and "coffee" in prompt


@pytest.mark.asyncio
async def test_decide_no_returns_empty(monkeypatch):
    async def fake_complete(system, user, *, timeout=120.0):
        return '{"need": false, "prompt": ""}'
    monkeypatch.setattr("services.ai_claude.complete", fake_complete)
    need, prompt = await va_image.decide_illustration("Короткая цитата дня про упорство в делах", "цитата")
    assert need is False and prompt == ""


@pytest.mark.asyncio
async def test_decide_failopen_on_error(monkeypatch):
    async def boom(system, user, *, timeout=120.0):
        raise RuntimeError("no model")
    monkeypatch.setattr("services.ai_claude.complete", boom)
    assert await va_image.decide_illustration("Любой достаточно длинный текст поста тут", "") == (False, "")


@pytest.mark.asyncio
async def test_decide_skips_too_short():
    assert await va_image.decide_illustration("ок", "") == (False, "")


# ── generate_image_bytes ──────────────────────────────────────────────────────

class _Prov:
    def __init__(self, name):
        self.name = name
        self.api_key = "k"
        self.base_url = "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_generate_uses_openai_first(monkeypatch):
    monkeypatch.setattr("services.ai_providers.configured_providers",
                        lambda: [_Prov("openai"), _Prov("gemini")])

    async def fake_openai(provider, prompt):
        assert provider.name == "openai"
        return b"IMGDATA"
    monkeypatch.setattr(va_image, "_openai_image", fake_openai)
    out = await va_image.generate_image_bytes("a cat")
    assert out == b"IMGDATA"


@pytest.mark.asyncio
async def test_generate_failopen_when_no_provider(monkeypatch):
    monkeypatch.setattr("services.ai_providers.configured_providers", lambda: [])
    assert await va_image.generate_image_bytes("a cat") is None


@pytest.mark.asyncio
async def test_generate_empty_prompt_is_none():
    assert await va_image.generate_image_bytes("") is None


# ── Интеграция в op_worker (доставка по media_prompt) ─────────────────────────

def test_op_worker_generates_from_media_prompt():
    from services import op_worker
    src = inspect.getsource(op_worker)
    assert "media_prompt" in src and "va_image.generate_image_bytes" in src, (
        "op_worker не генерирует картинку по media_prompt")


# ── Интеграция в publish (решение + media_prompt в операцию) ──────────────────

@pytest.mark.asyncio
async def test_publish_adds_media_prompt_when_illustration_needed(monkeypatch):
    from services import channel_admin as ca

    captured = {}

    async def fake_eligible(pool, owner_id, channel_id):
        return [1]

    async def fake_channel_row(pool, owner_id, channel_id):
        return {"title": "Кафе"}

    class _VM:
        MediaError = ca.__dict__.get("ChannelAdminError", Exception)

        @staticmethod
        async def get_photo(*a, **k):
            return None

        @staticmethod
        async def choose_photo(*a, **k):
            return None

        @staticmethod
        def caption_fits(t):
            return True

    async def fake_decide(text, pillar=""):
        return True, "nice photo prompt"

    async def fake_submit(pool, owner_id, op_type, params, **k):
        captured["params"] = params
        return 555

    monkeypatch.setattr(ca, "eligible_accounts" if hasattr(ca, "eligible_accounts") else "channel_row",
                        fake_channel_row, raising=False)
    monkeypatch.setattr(ca, "channel_row", fake_channel_row)
    monkeypatch.setattr("services.va_control.eligible_accounts", fake_eligible)
    monkeypatch.setattr("services.va_media.get_photo", _VM.get_photo)
    monkeypatch.setattr("services.va_media.choose_photo", _VM.choose_photo)
    monkeypatch.setattr("services.va_media.caption_fits", _VM.caption_fits)
    monkeypatch.setattr("services.va_image.decide_illustration", fake_decide)
    monkeypatch.setattr("services.operation_bus.submit", fake_submit)

    op_id = await ca.publish(None, 7, 8, "Достаточно длинный текст поста про кофе", "анонс")
    assert op_id == 555
    assert captured["params"].get("media_prompt") == "nice photo prompt", \
        "media_prompt не попал в операцию публикации"
    assert "require_media" not in captured["params"], \
        "ИИ-картинка не должна быть обязательной (сбой генерации → текстом)"
