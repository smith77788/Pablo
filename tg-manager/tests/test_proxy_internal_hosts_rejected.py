"""Прокси владельца не может указывать во внутреннюю сеть сервера.

Сервер сам подключается к прокси: проверка живости, Telethon, запросы через
него. Проверка искала подстроки «127.0.0.1»/«localhost», поэтому 10.0.0.5,
169.254.169.254 (метаданные облака), 2130706433 или имя, резолвящееся во
внутренний адрес, проходили: «проверка прокси» становилась сканером портов
сети сервера, а внутренний HTTP-прокси — выходом в неё. В боте и в импорте
сессий проверки не было совсем.

Тесты падают без фикса.
"""
from __future__ import annotations

import socket

import pytest

from services import security as sec
from services.mini_app_api import _reject_proxy_reason
from services.proxy_selector import is_safe_proxy_url

INTERNAL = [
    "socks5://10.0.0.5:1080",
    "http://169.254.169.254:80",
    "socks5://user:pw@192.168.1.10:1080",
    "socks5://2130706433:1080",
    "socks5://0x7f000001:1080",
    "socks5://[::ffff:127.0.0.1]:1080",
    "http://db.internal:3128",
    "10.1.2.3:1080",
]


@pytest.mark.parametrize("url", INTERNAL)
def test_literal_internal_hosts_are_rejected(url):
    assert sec.proxy_host_problem(url) == "internal"
    assert _reject_proxy_reason(url if "://" in url else "socks5://" + url) == "internal"
    assert not is_safe_proxy_url(url if "://" in url else "socks5://" + url)


def test_public_hosts_pass():
    for url in ("socks5://1.2.3.4:1080", "http://u:p@8.8.8.8:3128", "socks5://proxy.example.com:1"):
        assert sec.proxy_host_problem(url) is None


async def test_name_resolving_inside_is_rejected(monkeypatch):
    async def fake_getaddrinfo(self, host, *a, **k):
        ip = "10.9.9.9" if host == "evil.example" else "1.2.3.4"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    import asyncio
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
    assert not await sec.proxy_url_is_public("socks5://evil.example:1080")
    assert await sec.proxy_url_is_public("socks5://good.example:1080")
    assert await sec.public_proxy_flags(
        ["socks5://evil.example:1", "socks5://good.example:1"]) == [False, True]


def test_every_add_path_checks_dns():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    bot = (root / "bot/handlers/proxy_manager.py").read_text(encoding="utf-8")
    assert "proxy_url_is_public(url)" in bot and "public_proxy_flags(" in bot
    imp = (root / "services/session_importer.py").read_text(encoding="utf-8")
    assert "proxy_url_is_public(proxy_url)" in imp
    mini = (root / "services/mini_app_proxies.py").read_text(encoding="utf-8")
    assert "proxy_url_is_public(proxy_url)" in mini and "public_proxy_flags(purls)" in mini
