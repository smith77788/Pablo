"""Юнит-тесты «мягкого» инвайта — рассылка ссылки-приглашения в ЛС.

Заглушаем только account_manager.send_dm — проверяем подстановку {link},
подсчёт ok/failed, обработку приватности и стоп по peer_flood.
"""
from __future__ import annotations

import asyncio

import pytest

from services import mass_inviter_engine as inv


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_link_delivered_counts_ok(monkeypatch):
    sent = []

    async def fake_send_dm(session, username, text, _acc=None):
        sent.append((username, text))
        return {"ok": True}

    monkeypatch.setattr("services.account_manager.send_dm", fake_send_dm)
    # без пауз
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(inv.asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    res = _run(inv.invite_via_link_batch("s", {"id": 1}, "https://t.me/+ABC",
                                         ["@u1", "@u2", "123"]))
    assert res["ok"] == 3 and res["failed"] == 0
    # ссылка реально подставлена в каждое сообщение
    assert all("https://t.me/+ABC" in t for _, t in sent)


def test_custom_template_and_privacy(monkeypatch):
    async def fake_send_dm(session, username, text, _acc=None):
        if username == "@closed":
            return {"error": "приватность: пользователь запретил входящие"}
        return {"ok": True}

    monkeypatch.setattr("services.account_manager.send_dm", fake_send_dm)
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(inv.asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    res = _run(inv.invite_via_link_batch(
        "s", {"id": 1}, "L", ["@ok", "@closed"],
        message_text="Заходи: {link} — ждём!"))
    assert res["ok"] == 1 and res["failed"] == 1
    assert "@closed" in res["privacy_failed"]


def test_peer_flood_stops_batch(monkeypatch):
    calls = {"n": 0}

    async def fake_send_dm(session, username, text, _acc=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True}
        return {"error": "PeerFlood", "peer_flood": True}

    monkeypatch.setattr("services.account_manager.send_dm", fake_send_dm)
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(inv.asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    res = _run(inv.invite_via_link_batch("s", {"id": 1}, "L",
                                         ["@a", "@b", "@c", "@d"]))
    assert res["peer_flood"] is True
    # после peer_flood батч прекращён — не пытались слать всем
    assert calls["n"] == 2


def test_template_without_placeholder_appends_link(monkeypatch):
    sent = []

    async def fake_send_dm(session, username, text, _acc=None):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr("services.account_manager.send_dm", fake_send_dm)
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(inv.asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    _run(inv.invite_via_link_batch("s", {"id": 1}, "LINK", ["@u"],
                                   message_text="Без плейсхолдера"))
    # ссылка всё равно добавлена (движок дописывает {link}, если его забыли)
    assert "LINK" in sent[0]
