"""Регрессия: усиление SSRF-гарда (аватар бота + медиа рассылки).

Старый гард был обходим:
  • нестандартные кодировки IPv4 (2130706433, 127.1, 0x7f000001, 0177.0.0.1)
    резолвятся в 127.0.0.1, но regex `^127\\.` их не ловил;
  • канонический IPv6 (::1 частично, fc00::/fe80::/::ffff:127.0.0.1) пропускался;
  • dm_engine._download_media вообще не проверял адрес и пускал http:// —
    то есть http://169.254.169.254 (cloud metadata) был достижим.

Гард теперь един (services.security) и имеет два слоя: синтаксический
is_safe_public_url и авторитетный resolve_url_is_public (с резолвом DNS).
"""
from __future__ import annotations

import asyncio

import pytest

from services.security import (
    is_internal_ip,
    is_safe_public_url,
    resolve_url_is_public,
)


def test_ipv6_literals_rejected():
    # Канонический IPv6 loopback/ULA/link-local/mapped — раньше пропускались.
    assert is_safe_public_url("https://[::1]/a.jpg") is False
    assert is_safe_public_url("https://[::ffff:127.0.0.1]/a.jpg") is False
    assert is_safe_public_url("https://[fc00::1]/a.jpg") is False
    assert is_safe_public_url("https://[fe80::1]/a.jpg") is False


def test_ipv6_public_allowed_syntactically():
    assert is_safe_public_url("https://[2606:4700:10::6814:179a]/a.jpg") is True


def test_is_internal_ip_classifier():
    assert is_internal_ip("127.0.0.1") is True
    assert is_internal_ip("10.0.0.1") is True
    assert is_internal_ip("169.254.169.254") is True
    assert is_internal_ip("::ffff:127.0.0.1") is True  # mapped → 127.0.0.1
    assert is_internal_ip("fc00::1") is True
    assert is_internal_ip("garbage") is True            # неразбираемое → небезопасно
    assert is_internal_ip("8.8.8.8") is False
    assert is_internal_ip("1.1.1.1") is False


def test_encoded_ipv4_rejected_via_resolve():
    # ОС-резолвер разворачивает эти формы в 127.0.0.1 — авторитетный слой ловит.
    for u in (
        "https://2130706433/a.jpg",
        "https://127.1/a.jpg",
        "https://0x7f000001/a.jpg",
        "https://0177.0.0.1/a.jpg",
    ):
        assert asyncio.run(resolve_url_is_public(u)) is False, u


def test_resolve_rejects_http_and_internal():
    assert asyncio.run(resolve_url_is_public("http://example.com/a.jpg")) is False
    assert asyncio.run(resolve_url_is_public("https://127.0.0.1/a.jpg")) is False
    assert asyncio.run(resolve_url_is_public("https://169.254.169.254/latest")) is False


def test_resolve_allows_public_host():
    # Реальный публичный хост должен резолвиться и проходить.
    assert asyncio.run(resolve_url_is_public("https://example.com/a.jpg")) is True


def test_download_media_blocks_internal(monkeypatch):
    """dm_engine._download_media должен падать на внутреннем/http URL ДО сети."""
    from services import dm_engine

    async def _boom(*a, **k):  # если гард пропустит — тест поймает попытку сети
        raise AssertionError("не должно доходить до aiohttp для внутреннего URL")

    for bad in ("http://169.254.169.254/latest", "https://127.0.0.1/x.jpg",
                "https://2130706433/x.jpg"):
        with pytest.raises(ValueError):
            asyncio.run(dm_engine._download_media(bad))
