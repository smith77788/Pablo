"""Авто-ретрай AUTH_KEY_DUPLICATED в connect_client.

Симптом «свежий флот без прокси не стартует, 26/28 не ответили»: первый коннект
операции мог идти с другого IP, чем логин, и «лишний» коннект ещё закрывался →
AUTH_KEY_DUPLICATED. Раньше аккаунт сразу списывался. Теперь connect_client сам
переподключается с бэк-оффом — конфликт временный и снимается за 1–2 попытки.

telethon в песочнице застаблен (conftest отдаёт настоящие классы-исключения),
поэтому `except AuthKeyDuplicatedError` и `raise` совпадают по классу.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_manager as am
from telethon.errors import AuthKeyDuplicatedError


class _FakeClient:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_sleep(monkeypatch, sink):
    _orig = asyncio.sleep

    async def _fast(d):
        sink.append(d)
        await _orig(0)

    monkeypatch.setattr(am.asyncio, "sleep", _fast)


def test_retries_then_succeeds(monkeypatch):
    state = {"connect": 0, "make": 0}
    sleeps: list = []

    def fake_make(session, device, low_risk=False, _no_pool=False):
        state["make"] += 1
        return _FakeClient()

    async def fake_connect(client, acc, action_type):
        state["connect"] += 1
        if state["connect"] <= 2:          # первые 2 попытки — дубликат
            raise AuthKeyDuplicatedError()
        return                              # 3-я успешна

    monkeypatch.setattr(am, "_make_client", fake_make)
    monkeypatch.setattr(am, "_connect_and_track", fake_connect)
    _patch_sleep(monkeypatch, sleeps)

    client = _run(am.connect_client("sess", {"id": 1}))
    assert isinstance(client, _FakeClient)
    assert state["connect"] == 3                  # 2 провала + успех
    assert state["make"] == 3                      # новый клиент на каждую попытку
    assert sleeps == list(am._AUTH_DUP_BACKOFF)    # ждали оба бэк-оффа


def test_succeeds_first_try_no_sleep(monkeypatch):
    sleeps: list = []
    monkeypatch.setattr(am, "_make_client",
                        lambda *a, **k: _FakeClient())

    async def ok(client, acc, action_type):
        return

    monkeypatch.setattr(am, "_connect_and_track", ok)
    _patch_sleep(monkeypatch, sleeps)
    client = _run(am.connect_client("sess", {"id": 2}))
    assert isinstance(client, _FakeClient)
    assert sleeps == []                            # без конфликта — без пауз


def test_exhausts_retries_reraises(monkeypatch):
    sleeps: list = []
    made: list = []

    def fake_make(session, device, low_risk=False, _no_pool=False):
        c = _FakeClient()
        made.append(c)
        return c

    async def always_dup(client, acc, action_type):
        raise AuthKeyDuplicatedError()

    monkeypatch.setattr(am, "_make_client", fake_make)
    monkeypatch.setattr(am, "_connect_and_track", always_dup)
    _patch_sleep(monkeypatch, sleeps)

    with pytest.raises(AuthKeyDuplicatedError):
        _run(am.connect_client("sess", {"id": 3}))
    # попыток = ретраи + 1; пауз = число ретраев
    assert len(made) == len(am._AUTH_DUP_BACKOFF) + 1
    assert sleeps == list(am._AUTH_DUP_BACKOFF)
    # каждый неуспешный клиент отключён (не течём коннектами)
    assert all(c.disconnected for c in made)
