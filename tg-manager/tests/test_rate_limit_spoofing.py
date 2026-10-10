"""Ограничение частоты нельзя обойти подделкой X-Forwarded-For.

Заголовок дописывается прокси СПРАВА, слева клиент пишет что хочет. Раньше
_client_ip брал самое левое значение — то есть полностью подконтрольное
клиенту. Новый X-Forwarded-For на каждый запрос давал каждый раз чистый лимит
(вход, обмен кода связывания — всё без ограничения), а словарь лимитера рос с
каждым выдуманным адресом до исчерпания памяти.
"""
from __future__ import annotations

import asyncio
import importlib
import time

import pytest


def _sec(monkeypatch, **env):
    """Перечитать модуль с нужным окружением (константы читаются при импорте)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import services.security as s
    return importlib.reload(s)


class _Req:
    """Минимальный запрос: заголовки + адрес соединения."""

    def __init__(self, headers=None, peer="203.0.113.7"):
        self.headers = headers or {}
        self._peer = peer

    @property
    def transport(self):
        outer = self

        class _T:
            def get_extra_info(self, _name):
                return (outer._peer, 12345)

        return _T()


def test_spoofed_left_hand_values_are_ignored(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="1")
    # Клиент подставил чужой адрес слева, прокси дописал настоящий справа.
    req = _Req({"X-Forwarded-For": "1.2.3.4, 198.51.100.9"})
    assert s._client_ip(req) == "198.51.100.9"

    # Сколько бы значений клиент ни выдумал — берём то, что дописал наш прокси.
    req2 = _Req({"X-Forwarded-For": "9.9.9.9, 8.8.8.8, 7.7.7.7, 198.51.100.9"})
    assert s._client_ip(req2) == "198.51.100.9"


def test_forged_header_does_not_reset_the_limit(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="1", RATE_LIMIT_ENABLED="true")

    async def _go():
        allowed = 0
        for i in range(10):
            # На каждый запрос клиент выдумывает новый левый адрес.
            req = _Req({"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.9"})
            if await s.check_rate_limit(req, None, max_requests=3, window=60):
                allowed += 1
        return allowed

    assert asyncio.run(_go()) == 3, "лимит должен считаться по одному адресу"


def test_hops_zero_ignores_header_entirely(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="0")
    req = _Req({"X-Forwarded-For": "1.2.3.4, 198.51.100.9"}, peer="203.0.113.7")
    assert s._client_ip(req) == "203.0.113.7"


def test_two_hops_reads_second_from_the_right(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="2")
    req = _Req({"X-Forwarded-For": "1.2.3.4, 198.51.100.9, 192.0.2.1"})
    assert s._client_ip(req) == "198.51.100.9"


def test_garbage_and_short_headers_fall_back_to_connection(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="2")
    # Значений меньше, чем прокси, — заголовку верить нельзя.
    assert s._client_ip(_Req({"X-Forwarded-For": "1.2.3.4"})) == "203.0.113.7"
    # Не адрес, а мусор (или попытка подсунуть ключ лимитера).
    s2 = _sec(monkeypatch, TRUSTED_PROXY_HOPS="1")
    assert s2._client_ip(_Req({"X-Forwarded-For": "not-an-ip"})) == "203.0.113.7"
    assert s2._client_ip(_Req({"X-Forwarded-For": "999.1.1.1"})) == "203.0.113.7"


def test_ipv6_is_accepted(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="1")
    assert s._client_ip(_Req({"X-Forwarded-For": "2001:db8::1"})) == "2001:db8::1"


def test_limiter_does_not_grow_without_bound(monkeypatch):
    s = _sec(monkeypatch, RATE_LIMIT_ENABLED="true", RATE_LIMIT_MAX_KEYS="1000")

    async def _go():
        lim = s._RateLimiter()
        # Окно в 1 секунду: второй проход идёт уже за его пределами.
        for i in range(500):
            await lim.check(f"ip:10.1.{i // 256}.{i % 256}", 100, 1)
        assert len(lim._requests) == 500
        await asyncio.sleep(1.1)
        await lim.check("ip:198.51.100.9", 100, 1)
        return lim

    lim = asyncio.run(_go())
    assert len(lim._requests) == 1, "протухшие ключи должны вычищаться"


def test_limiter_caps_keys_even_inside_the_window(monkeypatch):
    s = _sec(monkeypatch, RATE_LIMIT_ENABLED="true")

    async def _go():
        lim = s._RateLimiter()
        now = time.time()
        for i in range(s._RATE_LIMIT_MAX_KEYS + 500):
            lim._requests[f"ip:x{i}"] = [now + i]
        lim._sweep(now + s._RATE_LIMIT_MAX_KEYS + 1000, window=10 ** 9)
        return lim

    lim = asyncio.run(_go())
    assert len(lim._requests) <= s._RATE_LIMIT_MAX_KEYS


def test_user_key_prefers_user_over_address(monkeypatch):
    s = _sec(monkeypatch, TRUSTED_PROXY_HOPS="1")
    req = _Req({"X-Forwarded-For": "1.2.3.4, 198.51.100.9"})
    assert s._user_key(req, 42) == "u:42"
    assert s._user_key(req, None) == "ip:198.51.100.9"
