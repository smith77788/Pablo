"""Вход и связывание устройства ограничиваются строже остальных маршрутов.

RATE_LIMIT_AUTH_MAX был объявлен и настраивался переменной окружения, но не
использовался нигде: вход и обмен кода связывания жили под общим лимитом в 120
запросов в минуту. Счётчик для них отдельный — иначе обычный трафик приложения
съедал бы лимит входа и наоборот.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


def _sec(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import services.security as s
    return importlib.reload(s)


class _Req:
    def __init__(self, path="/api/miniapp/dashboard", peer="203.0.113.7"):
        self.path = path
        self.headers = {}
        self._peer = peer

    @property
    def transport(self):
        outer = self

        class _T:
            def get_extra_info(self, _name):
                return (outer._peer, 12345)

        return _T()


def test_auth_paths_are_listed():
    import services.security as s
    assert "/api/miniapp/auth" in s.AUTH_RATE_LIMIT_PATHS
    assert "/api/miniapp/pair" in s.AUTH_RATE_LIMIT_PATHS
    assert "/api/miniapp/pair/exchange" in s.AUTH_RATE_LIMIT_PATHS


def test_auth_limit_is_stricter_and_actually_applied(monkeypatch):
    s = _sec(monkeypatch, RATE_LIMIT_ENABLED="true",
             RATE_LIMIT_AUTH_MAX="3", RATE_LIMIT_MAX_REQUESTS="120")

    async def _go():
        allowed = 0
        for _ in range(10):
            if await s.check_rate_limit_for_path(_Req("/api/miniapp/pair"), None):
                allowed += 1
        return allowed

    assert asyncio.run(_go()) == 3


def test_auth_counter_is_separate_from_the_general_one(monkeypatch):
    s = _sec(monkeypatch, RATE_LIMIT_ENABLED="true",
             RATE_LIMIT_AUTH_MAX="2", RATE_LIMIT_MAX_REQUESTS="120")

    async def _go():
        # Обычный трафик приложения не должен расходовать лимит входа.
        for _ in range(50):
            await s.check_rate_limit_for_path(_Req("/api/miniapp/dashboard"), None)
        return [await s.check_rate_limit_for_path(_Req("/api/miniapp/auth"), None)
                for _ in range(3)]

    assert asyncio.run(_go()) == [True, True, False]


def test_ordinary_paths_keep_the_general_limit(monkeypatch):
    s = _sec(monkeypatch, RATE_LIMIT_ENABLED="true",
             RATE_LIMIT_AUTH_MAX="2", RATE_LIMIT_MAX_REQUESTS="5")

    async def _go():
        allowed = 0
        for _ in range(10):
            if await s.check_rate_limit_for_path(_Req(), None):
                allowed += 1
        return allowed

    assert asyncio.run(_go()) == 5


def test_middleware_uses_the_path_aware_check():
    import services.security as s
    src = open(s.__file__, encoding="utf-8").read()
    start = src.index("def security_middleware(")
    body = src[start:start + 4000]
    assert "check_rate_limit_for_path(" in body, (
        "миддлварь снова считает вход по общему лимиту")
