"""Регресс: аккаунт БЕЗ назначенного прокси должен стартовать даже когда прокси из
free-pool не отвечает — переподключением НАПРЯМУЮ (host IP) под allow_direct.

Корень жалобы пользователя: аккаунты подключены недавно, прокси им не назначались,
но операции (bulk_join #117, mass_invite #119) не выполняются — «падение прокси/сессии».
Причина: у безпроксёвого аккаунта транспорт по цепочке падает в free-pool публичных
SOCKS5. Пул валидируется на скрейпе, но прокси умирают между циклами — аккаунт залипал
на мёртвом прокси, коннект падал, операция вставала. Прямого fallback не было.

connect_client теперь: если шли через 'pool', политика allow_direct и коннект упал
сетевой ошибкой — один раз переподключаемся напрямую. Bound-прокси и strict не трогаем.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_manager as am


def test_direct_fallback_ok_matrix():
    # БЕЗПРОКСЁВЫЕ транспорты relay/ipv6 откатываются на прямой host-IP (allow_direct):
    assert am._direct_fallback_ok("relay", "allow_direct") is True   # лежащий CF-релей → host-IP
    assert am._direct_fallback_ok("ipv6", "allow_direct") is True    # недоступный IPv6 → host-IP
    # strict и bound-прокси — НЕ откатываем:
    assert am._direct_fallback_ok("relay", "strict") is False        # strict запрещает host IP
    assert am._direct_fallback_ok("bound", "allow_direct") is False  # смена IP ломает auth key
    assert am._direct_fallback_ok("direct", "allow_direct") is False  # уже прямой — фолбэкать некуда
    assert am._direct_fallback_ok(None, "allow_direct") is False
    # free-pool удалён — транспорта 'pool' больше нет:
    assert am._direct_fallback_ok("pool", "allow_direct") is False


class _FakeClient:
    def __init__(self, transport):
        self._infragram_transport = transport
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def _install(monkeypatch, policy, first_transport, direct_ok=True):
    calls = {"make_no_pool": [], "connect": 0}

    def fake_make(session, device=None, low_risk=False, _no_pool=False, _force_direct=False):
        calls["make_no_pool"].append(_no_pool)
        # _force_direct/_no_pool → реально прямой транспорт (минуя релей/пул/IPv6).
        return _FakeClient("direct" if (_no_pool or _force_direct) else first_transport)

    async def fake_connect(client, device, action):
        calls["connect"] += 1
        if client._infragram_transport != "direct":
            raise OSError("pool proxy dead")
        if not direct_ok:
            raise OSError("direct also failed")
        return None

    monkeypatch.setattr(am, "_make_client", fake_make)
    monkeypatch.setattr(am, "_connect_and_track", fake_connect)
    monkeypatch.setattr(am, "_effective_proxy_policy", lambda d: policy)
    return calls


def test_no_proxy_account_falls_back_to_direct(monkeypatch):
    # free-pool удалён: безпроксёвый аккаунт без релея/IPv6 идёт СРАЗУ прямым host-IP.
    calls = _install(monkeypatch, "allow_direct", "direct")
    client = asyncio.run(am.connect_client("sess", {"id": 1}, "join"))
    assert client._infragram_transport == "direct"
    assert calls["connect"] == 1                          # прямой сразу, без промежуточного пула


def test_relay_falls_back_to_direct(monkeypatch):
    # Регресс «раньше работало на host-IP»: авто-раздача cf_relay_url увела флот на
    # CF-релей; лежащий релей теперь откатывается на прямой host-IP, а не падает.
    calls = _install(monkeypatch, "allow_direct", "relay")
    client = asyncio.run(am.connect_client("sess", {"id": 1}, "join"))
    assert client._infragram_transport == "direct"
    assert calls["make_no_pool"] == [False, True]         # релей → прямой host-IP
    assert calls["connect"] == 2


def test_ipv6_falls_back_to_direct(monkeypatch):
    calls = _install(monkeypatch, "allow_direct", "ipv6")
    client = asyncio.run(am.connect_client("sess", {"id": 1}, "join"))
    assert client._infragram_transport == "direct"
    assert calls["connect"] == 2


def test_strict_does_not_fall_back(monkeypatch):
    calls = _install(monkeypatch, "strict", "pool")
    with pytest.raises(OSError):
        asyncio.run(am.connect_client("sess", {"id": 1}, "join"))
    assert calls["make_no_pool"] == [False]              # прямого ретрая НЕ было


def test_bound_proxy_does_not_fall_back(monkeypatch):
    calls = _install(monkeypatch, "allow_direct", "bound")
    with pytest.raises(OSError):
        asyncio.run(am.connect_client("sess", {"id": 1}, "join"))
    assert calls["make_no_pool"] == [False]              # bound-прокси не переводим на host IP


def test_direct_first_time_succeeds_no_double_connect(monkeypatch):
    calls = _install(monkeypatch, "allow_direct", "direct")
    client = asyncio.run(am.connect_client("sess", {"id": 1}, "join"))
    assert client._infragram_transport == "direct"
    assert calls["connect"] == 1                         # прямой сработал сразу
    assert calls["make_no_pool"] == [False]
