"""Регрессия: медиа в DM-рассылке.

send_dm был text-only. Теперь при media_url отправка идёт через
send_media_via_account (Telethon send_file), текст — подпись. Без media_url —
прежний путь send_message.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.dm_engine import send_dm


@pytest.mark.asyncio
async def test_text_only_uses_send_message():
    with patch("services.account_manager.send_message", new=AsyncMock(return_value=True)) as sm, \
         patch("services.account_manager.send_media_via_account", new=AsyncMock(return_value=True)) as smed:
        r = await send_dm("sess", 123, "привет", username="alice")
    assert r["status"] == "sent"
    sm.assert_awaited()          # текст → send_message
    smed.assert_not_awaited()    # медиа не трогали


@pytest.mark.asyncio
async def test_media_uses_send_media():
    with patch("services.account_manager.send_message", new=AsyncMock(return_value=True)) as sm, \
         patch("services.account_manager.send_media_via_account", new=AsyncMock(return_value=True)) as smed:
        r = await send_dm("sess", 123, "подпись", username="alice",
                          media_url="https://example.com/p.jpg")
    assert r["status"] == "sent"
    smed.assert_awaited()        # медиа → send_media_via_account
    sm.assert_not_awaited()
    # текст передан как подпись (4-й позиционный или caption)
    args, kwargs = smed.await_args
    assert "подпись" in args or "подпись" in kwargs.values()


@pytest.mark.asyncio
async def test_media_send_failure_classified():
    with patch("services.account_manager.send_media_via_account",
               new=AsyncMock(side_effect=RuntimeError("UserPrivacyRestricted"))):
        r = await send_dm("sess", 123, "x", username="bob", media_url="https://e.com/a.jpg")
    assert r["status"] in ("skip", "retry", "blocked")
