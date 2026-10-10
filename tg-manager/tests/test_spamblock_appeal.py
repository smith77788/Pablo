"""Снятие спамблока (раздел 3 «Действие с аккаунтом» паритета TE).

Раньше спамблок можно было только ПРОВЕРИТЬ (check_account_status_full), но не
запросить снятие. appeal_spamblock проходит по кнопкам аппеляции @SpamBot
(«This is a mistake» → «Yes») — легитимная реабилитация СВОЕГО аккаунта, диалог
только с системным ботом Telegram. Тесты на мок-Telethon (без задержек).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import account_manager as am


class _Btn:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text, buttons=None):
        self.text = text
        self.buttons = buttons
        self.click = AsyncMock()


def _client_returning(msg_sequence):
    """Fake client: get_messages отдаёт заранее заданные сообщения по очереди."""
    client = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    seq = list(msg_sequence)

    async def _get_messages(entity, limit=1):
        return [seq.pop(0)] if seq else []

    client.get_messages = AsyncMock(side_effect=_get_messages)
    client.get_entity = AsyncMock(return_value=object())
    client.send_message = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def _run(coro):
    return asyncio.run(coro)


def test_no_session_short_string():
    res = _run(am.appeal_spamblock("x"))
    assert res["status"] == "no_session" and res["ok"] is False


def test_already_free():
    client = _client_returning([_Msg("Good news, no limits are applied to your account.")])
    with patch.object(am, "_make_client", return_value=client), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(am.appeal_spamblock("session-string-long-enough"))
    assert res["ok"] is True and res["status"] == "free" and res["steps"] == 0


def test_blocked_then_appeal_frees():
    blocked = _Msg("Your account is now limited.", buttons=[[_Btn("This is a mistake")]])
    freed = _Msg("Good news, no limits are currently applied.")
    client = _client_returning([blocked, freed])
    with patch.object(am, "_make_client", return_value=client), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(am.appeal_spamblock("session-string-long-enough"))
    assert res["ok"] is True and res["status"] == "free" and res["steps"] == 1
    # кнопка реально нажата
    blocked.click.assert_awaited()


def test_blocked_no_appeal_button():
    blocked = _Msg("Your account is limited. No buttons here.", buttons=None)
    client = _client_returning([blocked])
    with patch.object(am, "_make_client", return_value=client), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(am.appeal_spamblock("session-string-long-enough"))
    assert res["ok"] is True and res["status"] == "still_blocked" and res["steps"] == 0


def test_pick_appeal_button_matches():
    msg = _Msg("x", buttons=[[_Btn("Nope")], [_Btn("Это какая-то ошибка")]])
    assert am._pick_appeal_button(msg) == "Это какая-то ошибка"
    assert am._pick_appeal_button(_Msg("x", buttons=None)) is None


def test_route_and_engine_wired():
    import inspect
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    assert 'app.router.add_post("/api/miniapp/account/{acc_id}/spamblock_appeal", account_spamblock_appeal)' in src
    assert hasattr(am, "appeal_spamblock")
