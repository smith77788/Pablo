"""Экран «Каналы уведомлений» не ждёт Telegram на каждом открытии.

Имя главного бота (для ссылки «добавить в канал») бралось getMe, а неудача не
запоминалась: пока Telegram не отвечал, КАЖДОЕ открытие экрана ждало таймаут
запроса — на стенде ручка отвечала 3 с вместо миллисекунд. Теперь сначала
BOT_USERNAME из среды, удача кэшируется навсегда, неудача — на пять минут.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def mod(monkeypatch):
    from services import mini_app_notify_routes as m

    monkeypatch.setattr(m, "_bot_username_cache", {})
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    return m


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_failed_getme_is_not_retried_on_every_open(mod, monkeypatch):
    calls = []

    async def boom(method, **kw):
        calls.append(method)
        raise asyncio.TimeoutError

    monkeypatch.setattr(mod, "_main_call", boom)
    assert _run(mod._main_username()) == ""
    assert _run(mod._main_username()) == ""
    assert _run(mod._main_username()) == ""
    assert calls == ["getMe"], "каждое открытие экрана снова ждёт Telegram"


def test_failure_is_retried_after_the_pause(mod, monkeypatch):
    answers = [RuntimeError("нет сети"), {"result": {"username": "main_bot"}}]

    async def call(method, **kw):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(mod, "_main_call", call)
    assert _run(mod._main_username()) == ""
    mod._bot_username_cache["failed_at"] -= mod._USERNAME_RETRY_S + 1
    assert _run(mod._main_username()) == "main_bot"
    assert _run(mod._main_username()) == "main_bot"
    assert answers == []


def test_env_username_needs_no_telegram(mod, monkeypatch):
    async def never(method, **kw):
        raise AssertionError("в Telegram ходить не нужно")

    monkeypatch.setattr(mod, "_main_call", never)
    monkeypatch.setenv("BOT_USERNAME", "@from_env_bot")
    assert _run(mod._main_username()) == "from_env_bot"
