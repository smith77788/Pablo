"""Регрессия: отчёт кампании ЛС засчитывал недоставленные сообщения.

account_manager.send_message/send_media_via_account глотали почти все ошибки
(PeerFlood, закрытая личка, неразрешённый username, таймаут) и возвращали
False, а send_dm этот False не проверял и отвечал 'sent'. Предзапись в журнале
оставалась 'sent', и отчёт по аккаунтам показывал «✅ 10» там, где реально ушло
3–4. Аккаунт под спам-блоком при этом не выводился из ротации: PeerFlood до
классификатора не доходил.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from services import account_manager, dm_engine
from services.dm_engine import send_dm


class PeerFloodError(Exception):
    pass


@pytest.mark.asyncio
async def test_send_dm_asks_for_strict_delivery():
    with patch("services.account_manager.send_message",
               new=AsyncMock(return_value=True)) as sm:
        await send_dm("sess", 123, "привет", username="alice")
    assert sm.await_args.kwargs.get("strict") is True


@pytest.mark.asyncio
async def test_false_from_account_is_not_sent():
    with patch("services.account_manager.send_message",
               new=AsyncMock(return_value=False)):
        r = await send_dm("sess", 123, "привет")
    assert r["status"] != "sent"


@pytest.mark.asyncio
async def test_peer_flood_reaches_the_campaign():
    with patch("services.account_manager.send_message",
               new=AsyncMock(side_effect=PeerFloodError("PEER_FLOOD"))):
        r = await send_dm("sess", 123, "привет", username="alice")
    assert r["status"] == "peer_flood"


@pytest.mark.asyncio
async def test_timeout_is_unconfirmed_not_sent():
    with patch("services.account_manager.send_message",
               new=AsyncMock(side_effect=TimeoutError())):
        r = await send_dm("sess", 123, "привет")
    assert r["status"] == "unconfirmed"


@pytest.mark.asyncio
async def test_unresolved_username_falls_back_to_user_id():
    sm = AsyncMock(side_effect=[ValueError("No user has \"alice\""), True])
    with patch("services.account_manager.send_message", new=sm):
        r = await send_dm("sess", 123, "привет", username="alice")
    assert r["status"] == "sent"
    assert sm.await_args_list[1].args[1] == 123


class _Client:
    def __init__(self, exc):
        self._exc = exc

    async def send_message(self, *a, **k):
        raise self._exc

    async def send_file(self, *a, **k):
        raise self._exc

    async def connect(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [PeerFloodError("PEER_FLOOD"), TimeoutError()])
async def test_strict_account_send_raises_instead_of_false(exc):
    with patch.object(account_manager, "_make_client", return_value=_Client(exc)), \
         patch.object(account_manager, "_connect_and_track", new=AsyncMock()), \
         patch.object(account_manager, "safe_disconnect", new=AsyncMock()):
        # Без strict — прежний контракт для остальных вызывающих.
        assert await account_manager.send_message_via_account("s", 1, "t") is False
        with pytest.raises(type(exc)):
            await account_manager.send_message_via_account("s", 1, "t", strict=True)
        assert await account_manager.send_media_via_account(
            "s", 1, "https://e.com/a.jpg", "t") is False
        with pytest.raises(type(exc)):
            await account_manager.send_media_via_account(
                "s", 1, "https://e.com/a.jpg", "t", strict=True)


def test_unconfirmed_is_not_resent():
    with open(dm_engine.__file__, encoding="utf-8") as f:
        src = f.read()
    assert src.count("status IN ('sent','blocked','skip','unconfirmed')") >= 3
