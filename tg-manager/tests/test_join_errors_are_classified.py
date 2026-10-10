"""Регрессия: вступление в чат должно различать причины отказа.

`join_channel` разбирал ровно четыре ошибки Telegram, а всё остальное отдавал
наверх сырым английским текстом. Вызывающий не мог отличить:

  • «аккаунт уже в чате» — работать можно прямо сейчас;
  • «ссылка протухла» — перебирать флот бессмысленно, и со стороны это выглядит
    как долбёжка;
  • «аккаунт упёрся в потолок Telegram по числу чатов» — он не вступит НИКОГДА,
    пока откуда-нибудь не выйдет;
  • «заявка подана, ждём администратора» — каждая повторная попытка это ещё
    одна заявка, то есть спам в чужой чат.

Плюс единственным способом опознать «уже в чате» был поиск английского слова
«already» в тексте ошибки: любая правка формулировки молча превращала готовый
аккаунт в проваленный.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import account_manager as am
from services import invite_preflight as ip


def _named(name: str, text: str = "ошибка"):
    """Исключение с нужным именем класса — Telethon опознаётся по имени."""
    return type(name, (Exception,), {})(text)


def _join(exc):
    """Прогнать join_channel так, чтобы упало ровно нашим исключением."""
    async def _boom(*a, **kw):
        raise exc

    import unittest.mock as _m
    with _m.patch.object(am, "connect_client", _boom):
        return asyncio.run(am.join_channel("sess", "@chat", _acc={"id": 1}))


_PROPER = ("Telegram", "PeerFlood")


def _has_english(text: str) -> bool:
    t = text or ""
    for w in _PROPER:
        t = t.replace(w, "")
    return bool(re.search(r"[A-Za-z]{4,}", t))


def test_detector_self_check():
    assert not _has_english("Аккаунт уже состоит в этом чате.")
    assert _has_english("The user is already a participant")


# --- каждая причина опознаётся отдельным признаком ------------------------

def test_already_a_member():
    res = _join(_named("UserAlreadyParticipantError",
                       "The user is already a participant"))
    assert res.get("already_member") is True, res
    assert not _has_english(res["error"]), res["error"]


def test_already_a_member_by_text_only():
    """Тот же случай, пришедший обычным ValueError без своего типа."""
    res = _join(ValueError("The user is already a participant of the chat"))
    assert res.get("already_member") is True, res


def test_invite_link_is_dead():
    for name in ("InviteHashExpiredError", "InviteHashInvalidError",
                 "UsernameNotOccupiedError"):
        res = _join(_named(name))
        assert res.get("invite_invalid") is True, (name, res)
        assert not _has_english(res["error"]), res["error"]


def test_account_hit_the_chat_limit():
    res = _join(_named("UserChannelsTooMuchError"))
    assert res.get("channels_limit") is True, res
    assert not _has_english(res["error"]), res["error"]


def test_chat_limit_by_text_only():
    res = _join(ValueError("Too much channels joined (caused by JoinChannel)"))
    assert res.get("channels_limit") is True, res


def test_join_request_is_not_retried_blindly():
    res = _join(_named("InviteRequestSentError"))
    assert res.get("request_sent") is True, res
    assert not _has_english(res["error"]), res["error"]


# --- прежние случаи не потеряны ------------------------------------------

def test_flood_still_reports_the_wait():
    exc = _named("FloodWaitError", "A wait of 300 seconds is required")
    exc.seconds = 300
    res = _join(exc)
    assert res.get("flood_wait") == 300, res
    assert not _has_english(res["error"]), res["error"]


def test_peer_flood_is_not_a_channel_ban():
    res = _join(_named("PeerFloodError"))
    assert res.get("peer_flood") is True and not res.get("banned"), res


def test_ban_is_still_a_ban():
    for name in ("UserBannedInChannelError", "ChannelPrivateError"):
        res = _join(_named(name))
        assert res.get("banned") is True, (name, res)
        assert not _has_english(res["error"]), res["error"]


def test_unknown_error_is_not_swallowed():
    res = _join(ValueError("НЕЧТО СОВСЕМ ДРУГОЕ"))
    assert "error" in res and res["error"]
    assert not any(res.get(k) for k in
                   ("already_member", "invite_invalid", "channels_limit",
                    "request_sent", "banned", "peer_flood"))


# --- пре-флайт считает «уже в чате» по признаку, а не по слову ------------

def _preflight_with(result: dict) -> dict:
    async def _joiner(session_string, acc, group):
        return result

    class _Pool:
        async def fetch(self, *a, **kw):
            return [{"id": 1, "phone": "+7", "session_str": "s"}]

    return asyncio.run(
        ip.join_all(_Pool(), 10, "@chat", account_ids=[1], joiner=_joiner))


def test_preflight_counts_a_russian_already_member():
    out = _preflight_with({"error": "Аккаунт уже состоит в этом чате.",
                           "already_member": True})
    assert out["already"] == 1 and out["failed"] == 0, (
        f"готовый аккаунт посчитан проваленным: {out!r}"
    )


def test_preflight_still_reads_a_foreign_english_message():
    """Запасной путь: сообщение может прийти и из чужого joiner-а."""
    out = _preflight_with({"error": "The user is already a participant"})
    assert out["already"] == 1, out


def test_preflight_counts_a_real_failure():
    out = _preflight_with({"error": "Ссылка или адрес чата недействительны.",
                           "invite_invalid": True})
    assert out["failed"] == 1 and out["already"] == 0, out


def test_preflight_counts_a_success():
    out = _preflight_with({"channel_id": 5, "title": "Чат"})
    assert out["joined"] == 1, out
