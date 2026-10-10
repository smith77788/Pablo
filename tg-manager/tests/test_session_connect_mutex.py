"""Процессный мьютекс на СЕССИЮ в connect_client — защита от AUTH_KEY_DUPLICATED.

Корень «навсегда мёртвых» аккаунтов: одна сессия коннектится из двух мест разом
(операция + фоновый цикл/веб-валидация) → Telegram видит ключ с двух IP и отзывает
его безвозвратно. Арбитр операций закрывал лишь операция↔операция. Этот мьютекс —
единый порог для ЛЮБОГО коннекта: одну сессию в момент держит ровно один клиент.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_manager as am
from telethon.errors import AuthKeyDuplicatedError  # noqa: F401 (класс-заглушка)


class _FakeClient:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_session_key_is_by_session_string():
    # Ключ стабилен по сессии и не зависит от подсистемы/наличия id.
    k1 = am._session_key("SESSION_ABC", {"id": 1})
    k2 = am._session_key("SESSION_ABC", {"id": 999})   # тот же ключ → др. id
    assert k1 == k2 and k1.startswith("s:")
    assert am._session_key("OTHER", {"id": 1}) != k1
    # без сессии — падаем на id, пустой ключ если и его нет
    assert am._session_key("", {"id": 5}) == "a:5"
    assert am._session_key("", {}) == ""


def test_acquire_release_roundtrip():
    key = am._session_key("S1", None)
    assert am._try_acquire_session(key) is True
    assert am._try_acquire_session(key) is False   # уже занято
    am._release_session(key)
    assert am._try_acquire_session(key) is True     # освобождено
    am._release_session(key)


def test_connect_holds_until_disconnect(monkeypatch):
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _FakeClient())

    async def ok(client, acc, action_type):
        return

    monkeypatch.setattr(am, "_connect_and_track", ok)

    async def scenario():
        c1 = await am.connect_client("SHARED", {"id": 1})
        # Сессия занята первым клиентом → второй коннект той же сессии не проходит
        # (ждать нечего в тесте: budget=0 через retry_auth_dup=False).
        with pytest.raises(am.SessionBusyError):
            await am.connect_client("SHARED", {"id": 1}, retry_auth_dup=False)
        # Первый отключился → сессия освобождена → второй проходит.
        await c1.disconnect()
        c2 = await am.connect_client("SHARED", {"id": 1}, retry_auth_dup=False)
        assert isinstance(c2, _FakeClient)
        await c2.disconnect()

    _run(scenario())


def test_different_sessions_do_not_block(monkeypatch):
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _FakeClient())

    async def ok(client, acc, action_type):
        return

    monkeypatch.setattr(am, "_connect_and_track", ok)

    async def scenario():
        a = await am.connect_client("SESS_A", {"id": 1}, retry_auth_dup=False)
        b = await am.connect_client("SESS_B", {"id": 2}, retry_auth_dup=False)  # др. сессия — ок
        assert isinstance(a, _FakeClient) and isinstance(b, _FakeClient)
        await a.disconnect()
        await b.disconnect()

    _run(scenario())


def test_failed_connect_releases_session(monkeypatch):
    # Коннект упал → сессия освобождается (иначе следующая операция залипнет).
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _FakeClient())

    async def boom(client, acc, action_type):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(am, "_connect_and_track", boom)

    async def scenario():
        with pytest.raises(RuntimeError):
            await am.connect_client("SX", {"id": 1}, retry_auth_dup=False)
        # сессия свободна — повторный захват проходит
        assert am._try_acquire_session(am._session_key("SX", {"id": 1})) is True

    _run(scenario())


def test_busy_error_is_connection_error():
    # SessionBusyError — наследник ConnectionError: caller лечит как сеть (кулдаун+
    # повтор), НЕ как смерть аккаунта.
    assert issubclass(am.SessionBusyError, ConnectionError)


def test_raw_make_client_connect_holds_mutex(monkeypatch):
    # Централизованная защита: RAW-коннектор (_make_client(...).connect() мимо
    # connect_client) тоже держит мьютекс — второй коннект той же сессии не проходит,
    # пока первый не отключится. Закрывает десятки прямых callsite разом.
    monkeypatch.setattr(am, "_IPV6_SUBNET", "")
    monkeypatch.setattr(am, "CF_RELAY_URL", "")
    dev = {"id": 1, "device_model": "x", "system_version": "x", "app_version": "x",
           "lang_code": "en", "system_lang_code": "en", "proxy_policy": "allow_direct"}
    sess = "RAWSESSION_" + "A" * 120

    async def scenario():
        c1 = am._make_client(sess, dev)
        await c1.connect()                         # захватывает мьютекс сессии
        c2 = am._make_client(sess, dev)
        with pytest.raises(am.SessionBusyError):   # та же сессия занята → не коннектим
            await c2.connect()
        await c1.disconnect()                      # освобождает
        c3 = am._make_client(sess, dev)
        await c3.connect()                         # теперь свободно
        await c3.disconnect()

    _run(scenario())


def test_connect_client_client_not_double_wrapped(monkeypatch):
    # connect_client управляет мьютексом сам → его клиенты идут с _mutex_managed=True
    # и НЕ оборачиваются повторно (иначе двойной захват → self-SessionBusyError).
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _FakeClient())

    seen = {}
    _orig = am._make_client

    def _spy(*a, **k):
        seen["mutex_managed"] = k.get("_mutex_managed")
        return _FakeClient()

    monkeypatch.setattr(am, "_make_client", _spy)

    async def ok(client, acc, action_type):
        return

    monkeypatch.setattr(am, "_connect_and_track", ok)
    c = _run(am.connect_client("SESSX", {"id": 9}))
    assert isinstance(c, _FakeClient)
    assert seen.get("mutex_managed") is True
