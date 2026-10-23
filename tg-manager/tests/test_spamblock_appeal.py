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
from tests.miniapp_source import api_source


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


def test_confession_step_clicks_deny_not_yes_ru():
    # Реальный шаг-ловушка @SpamBot (скрин владельца): «Да, такие случаи были»
    # топит аккаунт навсегда. Жать можно только «Нет, никогда».
    msg = _Msg(
        "Подтвердите, что Вы никогда не отправляли незнакомым людям: Рекламу, "
        "которую они не запрашивали, Материалы для продвижения, Шок-контент. "
        "Вы отправляли что-нибудь подобное?",
        buttons=[[_Btn("Да, такие случаи были")], [_Btn("Нет, никогда")]],
    )
    assert am._is_spambot_confession(msg) is True
    assert am._pick_appeal_button(msg) == "Нет, никогда"


def test_confession_step_clicks_deny_en():
    msg = _Msg(
        "Please confirm you never sent strangers unsolicited Advertising, "
        "promotional materials, or shock content. Did you send anything like this?",
        buttons=[[_Btn("Yes, I did")], [_Btn("No, never")]],
    )
    assert am._is_spambot_confession(msg) is True
    assert am._pick_appeal_button(msg) == "No, never"


def test_confession_without_deny_button_clicks_nothing():
    # Нет кнопки отрицания — лучше оборвать аппеляцию, чем признать спам «Да».
    msg = _Msg(
        "Вы отправляли незнакомым рекламу или материалы для продвижения?",
        buttons=[[_Btn("Да, такие случаи были")]],
    )
    assert am._is_spambot_confession(msg) is True
    assert am._pick_appeal_button(msg) is None


def test_ordinary_appeal_step_still_clicks_yes():
    # Обычный шаг «Хотите отправить апелляцию?» — это НЕ ловушка (нет слов про
    # рекламу/спам), и «Да» здесь жать нужно.
    msg = _Msg(
        "Хотите отправить апелляцию?",
        buttons=[[_Btn("Да")], [_Btn("Нет")]],
    )
    assert am._is_spambot_confession(msg) is False
    assert am._pick_appeal_button(msg) == "Да"


def test_mistake_step_unaffected():
    msg = _Msg("Your account is now limited.",
               buttons=[[_Btn("This is a mistake")]])
    assert am._is_spambot_confession(msg) is False
    assert am._pick_appeal_button(msg) == "This is a mistake"


def test_route_and_engine_wired():
    import inspect
    from services import mini_app_api
    src = api_source()
    assert 'app.router.add_post("/api/miniapp/account/{acc_id}/spamblock_appeal", account_spamblock_appeal)' in src
    assert hasattr(am, "appeal_spamblock")
