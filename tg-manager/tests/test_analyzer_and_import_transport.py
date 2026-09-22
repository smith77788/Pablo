"""Разбор сущностей и проверка импортируемой сессии ходят своим выходом.

Два последних места, где клиент строился одной лишь строкой сессии.

`entity_analyzer._get_client` перебирает активные аккаунты владельца и отдаёт
первый подключившийся — на нём висят все три анализатора (канал, пользователь,
объект). Словарь аккаунта лежал в той же переменной, но в `_make_client` не
попадал, поэтому разбор уходил НАПРЯМУЮ с host-IP, пока рассылки и парсер того
же аккаунта шли через назначенный ему прокси.

`account_manager.validate_session_import` принимала `proxy_url` и молча его
выбрасывала: сессию проверяли с одного адреса, а работать ей потом с другого.

И то и другое — AUTH_KEY_DUPLICATED, после которого ключ отозван навсегда.

Заодно закрыт знакомый по QR-входу промах: подключённый клиент, на который ни
у кого нет ссылки. В переборе анализатора отмена внешним таймаутом проходила
мимо `except Exception`, а в проверке импорта `disconnect` стоял только на
успешном пути — сорвался `get_me`, и коннект оставался жить.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import account_manager as am
from services import entity_analyzer as ea


def _run(coro):
    return asyncio.run(coro)


def _client(*, connect_error=None, me=None):
    client = MagicMock()
    client.connect = AsyncMock(side_effect=connect_error)
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(return_value=me)
    return client


class _FakePool:
    pass


_ACC = {
    "id": 42, "session_str": "sess", "proxy_id": 7,
    "cf_relay_url": "https://relay.example", "owner_id": 9,
    "ipv6_subnet": "2a01:4f8::/64",
}


# ── анализатор сущностей ─────────────────────────────────────────────────────

def test_analyzer_builds_the_client_with_the_whole_account():
    client = _client()
    seen = {}

    def _capture(session_string, device=None, *a, **k):
        seen["session"], seen["device"] = session_string, device
        return client

    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[_ACC])):
        with patch("services.account_manager._make_client", side_effect=_capture):
            got = _run(ea._get_client(_FakePool(), 9))

    assert got is client
    assert seen["session"] == "sess"
    assert seen["device"] is not None, (
        "словарь аккаунта не передан — разбор пойдёт напрямую с host-IP, не тем "
        "выходом, что остальные подсистемы аккаунта"
    )
    for key in ("id", "proxy_id", "cf_relay_url", "ipv6_subnet"):
        assert seen["device"].get(key) == _ACC[key]


def test_analyzer_tries_the_next_account_and_closes_the_failed_one():
    dead, alive = _client(connect_error=OSError("сеть")), _client()
    clients = iter((dead, alive))
    second = {"id": 43, "session_str": "s2", "proxy_id": None, "cf_relay_url": ""}

    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[_ACC, second])):
        with patch("services.account_manager._make_client",
                   side_effect=lambda *a, **k: next(clients)):
            got = _run(ea._get_client(_FakePool(), 9))

    assert got is alive
    dead.disconnect.assert_awaited_once(), "неудачный коннект оставлен открытым"
    alive.disconnect.assert_not_awaited()


def test_analyzer_does_not_leak_the_client_on_cancellation():
    """Отмена — BaseException, мимо `except Exception` она проходит насквозь."""
    client = _client(connect_error=asyncio.CancelledError())

    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[_ACC])):
        with patch("services.account_manager._make_client", return_value=client):
            with pytest.raises(asyncio.CancelledError):
                _run(ea._get_client(_FakePool(), 9))

    client.disconnect.assert_awaited_once(), (
        "подключённый клиент потерян: ссылки на него нет ни у кого"
    )


def test_analyzer_returns_nothing_without_accounts():
    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[])):
        assert _run(ea._get_client(_FakePool(), 9)) is None


# ── проверка импортируемой сессии ────────────────────────────────────────────

def test_import_check_goes_through_the_given_proxy():
    me = MagicMock(phone="79990000000", id=555)
    client = _client(me=me)
    seen = {}

    def _capture(session_string, device=None, *a, **k):
        seen["device"] = device
        return client

    with patch.object(am, "_make_client", side_effect=_capture):
        res = _run(am.validate_session_import("sess", "socks5://u:p@host:1080"))

    assert res == {"valid": True, "phone": "79990000000", "user_id": 555}
    assert seen["device"] is not None, (
        "proxy_url принят и выброшен: проверка с одного адреса, работа с другого"
    )
    assert seen["device"]["proxy_url"] == "socks5://u:p@host:1080"


def test_import_check_does_not_borrow_another_accounts_transport():
    """Строки аккаунта ещё нет: релея и назначенного прокси у сессии нет
    по-настоящему, и `_make_client` не должен добирать их из карты."""
    client = _client(me=MagicMock(phone="", id=1))

    seen = {}
    with patch.object(am, "_make_client",
                      side_effect=lambda s, d=None, *a, **k: (seen.update(d=d), client)[1]):
        _run(am.validate_session_import("sess"))

    assert seen["d"]["proxy_id"] is None
    assert seen["d"]["cf_relay_url"] == ""


def test_import_check_closes_the_client_when_it_fails():
    client = _client(me=None)
    client.get_me = AsyncMock(side_effect=RuntimeError("сессия мертва"))

    with patch.object(am, "_make_client", return_value=client):
        res = _run(am.validate_session_import("sess"))

    assert res["valid"] is False
    client.disconnect.assert_awaited_once(), (
        "коннект остался жить и держит мьютекс сессии"
    )


def test_import_check_closes_the_client_when_it_succeeds():
    client = _client(me=MagicMock(phone="7999", id=2))

    with patch.object(am, "_make_client", return_value=client):
        _run(am.validate_session_import("sess"))

    client.disconnect.assert_awaited_once()
