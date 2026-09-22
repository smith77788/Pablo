"""Рассылка выводит из аудитории только тех, кто действительно ушёл.

ЧТО БЫЛО. На любую неудачу без `retry_after` цикл рассылки звал
`mark_user_inactive`, то есть ставил подписчику `is_active=FALSE` — а из
аудитории отбираются только активные, значит следующие рассылки его уже не
увидят. При этом `bot_api.send_message` отдавал `(False, None)` одинаково для:

  * 403 «bot was blocked by the user» — подписчик правда ушёл;
  * 400 «can't parse entities» — это НАШ текст с незакрытой HTML-скобкой;
  * 401 — токен бота отозван;
  * 500/502/503 — Telegram лежит;
  * сетевой сбой (`error_code: 0`) — до Telegram вообще не достучались.

Цена у первых четырёх случаев одинаковая и катастрофическая: одна кривая
разметка в тексте даёт 400 на КАЖДОГО получателя, и вся аудитория бота
выходит из строя разом — молча, за один прогон, без возможности понять
причину. Отозванный токен делает то же самое и вдобавок честно добивает все
10 000 вызовов.

Рядом, в админской рассылке (`bot/handlers/admin.py`), давно было правильно:
`if cat in ("blocked", "deactivated")`. Здесь этого различия просто не было.

Проверяем через настоящий `bot_api`: подменяется только `_call`, поэтому
классификация ответа Telegram работает по-боевому.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services import bot_api, broadcaster

TOKEN = "123456789:AAHrealtokenlooking-value_0123456789ab"
BOT_ID = 555
BC_ID = 77
USERS = [101, 102, 103]


def _run(coro):
    return asyncio.run(coro)


class _FakePool:
    """Минимум, который читает `broadcaster.run` до цикла отправки."""

    async def fetchval(self, query, *args):
        if "added_by FROM managed_bots" in query:
            return 1  # владелец есть — проверка доступа проходит
        return None

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"

    async def executemany(self, query, args):
        return None


def _reply(error_code: int, description: str):
    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT_ID, "username": "b"}}
        return {"ok": False, "error_code": error_code, "description": description}
    return _call


def _broadcast(error_code: int, description: str, users=None):
    """Прогнать рассылку, где Telegram на каждую отправку отвечает так.

    Возвращает список подписчиков, выведенных из аудитории.
    """
    dropped: list[int] = []

    async def _mark(pool, bot_id, uid):
        dropped.append(uid)

    calls: list[int] = []
    _orig = _reply(error_code, description)

    async def _counting(session, token, method, **params):
        if method != "getMe":
            calls.append(params.get("chat_id"))
        return await _orig(session, token, method, **params)

    with patch.object(bot_api, "_call", _counting):
        with patch("database.db.mark_user_inactive", _mark):
            with patch("database.db.update_broadcast", AsyncMock()):
                with patch("database.db.get_broadcast_delivered_ids",
                           AsyncMock(return_value=set())):
                    with patch("services.brand_injection.is_free_tier",
                               AsyncMock(return_value=False)):
                        _run(broadcaster.run(
                            _FakePool(), _FakeSession(), BC_ID, TOKEN, BOT_ID,
                            "Привет", user_ids=list(users or USERS),
                        ))
    return dropped, calls


class _FakeSession:
    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(broadcaster, "BROADCAST_DELAY", 0, raising=False)
    monkeypatch.setattr(broadcaster, "_GROUP_DELAY", 0, raising=False)


# ── аудитория не должна страдать за чужие беды ───────────────────────────────

def test_broken_markup_does_not_wipe_the_audience():
    """Одна незакрытая скобка в тексте — и раньше уходила вся аудитория."""
    dropped, calls = _broadcast(
        400, "Bad Request: can't parse entities: Unmatched end tag at byte offset 12")

    assert len(calls) == len(USERS), "рассылка должна пройти всех получателей"
    assert dropped == [], (
        f"из аудитории выведены живые подписчики {dropped} — причина была в "
        "нашем тексте, а не в них"
    )


def test_telegram_outage_does_not_wipe_the_audience():
    dropped, _ = _broadcast(502, "Bad Gateway")
    assert dropped == []


def test_network_failure_does_not_wipe_the_audience():
    """_call на сетевой сбой ставит error_code 0 и своё описание."""
    dropped, _ = _broadcast(0, "Network error after 3 retries: Cannot connect")
    assert dropped == []


# ── а кто действительно ушёл — выводится ─────────────────────────────────────

def test_blocked_subscriber_leaves_the_audience():
    dropped, _ = _broadcast(403, "Forbidden: bot was blocked by the user")
    assert dropped == USERS


def test_deleted_account_leaves_the_audience():
    dropped, _ = _broadcast(403, "Forbidden: user is deactivated")
    assert dropped == USERS


def test_never_started_leaves_the_audience():
    dropped, _ = _broadcast(400, "Bad Request: chat not found")
    assert dropped == USERS


# ── отозванный токен ─────────────────────────────────────────────────────────

def test_revoked_token_stops_the_broadcast():
    """Дальше вся аудитория получит ту же 401 — добивать её незачем."""
    dropped, calls = _broadcast(401, "Unauthorized")

    assert len(calls) == 1, (
        f"после отзыва токена сделано {len(calls)} попыток вместо одной"
    )
    assert dropped == [], "за отозванный токен подписчики не отвечают"


# ── классификация ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,desc,expected", [
    (429, "Too Many Requests", "flood"),
    (403, "Forbidden: bot was blocked by the user", "blocked"),
    (403, "Forbidden: user is deactivated", "deactivated"),
    (400, "Bad Request: chat not found", "not_started"),
    (400, "Bad Request: user not found", "not_started"),
    (401, "Unauthorized", "unauthorized"),
    (0, "Network error after 3 retries: timeout", "unavailable"),
    (500, "Internal Server Error", "unavailable"),
    (503, "Service Unavailable", "unavailable"),
    (400, "Bad Request: message text is empty", "bad_request"),
    (400, "Bad Request: can't parse entities", "bad_request"),
])
def test_classification(code, desc, expected):
    assert bot_api.classify_send_error(code, desc) == expected


def test_only_three_categories_take_a_subscriber_out():
    assert bot_api.RECIPIENT_GONE == {"blocked", "deactivated", "not_started"}
    for gone in bot_api.RECIPIENT_GONE:
        assert bot_api.recipient_is_gone(gone)
    for stays in ("flood", "unauthorized", "unavailable", "bad_request", "other", ""):
        assert not bot_api.recipient_is_gone(stays), (
            f"категория {stays!r} выводит живого подписчика из аудитории"
        )
