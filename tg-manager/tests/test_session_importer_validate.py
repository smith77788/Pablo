"""Регрессия: validate_session() должна честно использовать переданный proxy_url
и всегда закрывать клиента, даже если Telegram упал после connect().

До фикса proxy_url принимался, но никуда не передавался в _make_client (валидация
шла мимо запрошенного прокси), а client.disconnect() не вызывался, если get_me()
падал после успешного connect() — утечка соединения на каждой такой сессии.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.session_importer import validate_session


class _FakeMe:
    phone = "+123"
    id = 42
    first_name = "Test"
    username = "tester"


def _fake_client(connect_ok: bool = True, get_me_ok: bool = True) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock()
    if not connect_ok:
        client.connect.side_effect = RuntimeError("connect failed")
    client.get_me = AsyncMock(return_value=_FakeMe())
    if not get_me_ok:
        client.get_me.side_effect = RuntimeError("get_me failed")
    client.disconnect = AsyncMock()
    client.is_connected = MagicMock(return_value=True)
    return client


@pytest.mark.asyncio
async def test_proxy_url_forwarded_to_make_client():
    client = _fake_client()
    with patch("services.account_manager._make_client", return_value=client) as make_client:
        result = await validate_session("session-string", proxy_url="socks5://1.2.3.4:1080")
    assert result["valid"] is True
    make_client.assert_called_once_with("session-string", {"proxy_url": "socks5://1.2.3.4:1080"})


@pytest.mark.asyncio
async def test_no_proxy_url_passes_none_device():
    client = _fake_client()
    with patch("services.account_manager._make_client", return_value=client) as make_client:
        await validate_session("session-string")
    make_client.assert_called_once_with("session-string", None)


@pytest.mark.asyncio
async def test_disconnect_called_after_get_me_failure():
    client = _fake_client(get_me_ok=False)
    with patch("services.account_manager._make_client", return_value=client):
        result = await validate_session("session-string")
    assert result["valid"] is False
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_not_called_when_never_connected():
    client = _fake_client(connect_ok=False)
    client.is_connected.return_value = False
    with patch("services.account_manager._make_client", return_value=client):
        result = await validate_session("session-string")
    assert result["valid"] is False
    client.disconnect.assert_not_awaited()
