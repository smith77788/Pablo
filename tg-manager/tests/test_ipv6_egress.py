"""Свой IPv6 на аккаунт должен реально работать, а не только включаться.

1. Сессия, залогиненная по IPv4 на DC1/3/4/5, при переходе на IPv6 обязана
   остаться на своём DC. telethon при несовпадении семейства перекидывает её на
   DC2 со старым ключом → AUTH_KEY_UNREGISTERED, живой аккаунт выглядит мёртвым.
2. В подсети шире /64 каждый аккаунт получает свою /64: антиспам считает /64
   одним абонентом, раздача подряд из первой /64 показывала флот одним IP.
3. Сохранение подсети проверяет, что хост действительно ходит из неё в Telegram.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading

import pytest

from services import ipv6_egress as eg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Session:
    def __init__(self, dc_id, addr):
        self.dc_id, self.server_address, self.port = dc_id, addr, 443

    def set_dc(self, dc_id, addr, port):
        self.dc_id, self.server_address, self.port = dc_id, addr, port


@pytest.mark.parametrize("dc", [1, 2, 3, 4, 5])
def test_ipv4_session_keeps_its_dc_on_ipv6(dc):
    s = _Session(dc, eg.DC_IPV4[dc])
    assert eg.preserve_session_dc(s, use_ipv6=True)
    assert s.dc_id == dc
    assert s.server_address == eg.DC_IPV6[dc]
    assert ":" in s.server_address  # telethon больше не сбросит на DC2


def test_ipv6_session_keeps_its_dc_on_direct_fallback():
    s = _Session(4, eg.DC_IPV6[4])
    assert eg.preserve_session_dc(s, use_ipv6=False)
    assert (s.dc_id, s.server_address) == (4, eg.DC_IPV4[4])


def test_preserve_is_noop_when_nothing_to_fix():
    same = _Session(4, eg.DC_IPV4[4])
    assert not eg.preserve_session_dc(same, use_ipv6=False)
    assert same.server_address == eg.DC_IPV4[4]
    fresh = _Session(None, None)  # новая сессия логина
    assert not eg.preserve_session_dc(fresh, use_ipv6=True)
    unknown = _Session(10004, "149.154.167.40")  # тестовый DC — не трогаем
    assert not eg.preserve_session_dc(unknown, use_ipv6=True)
    assert unknown.server_address == "149.154.167.40"


def test_make_client_preserves_dc_before_connect():
    src = open(os.path.join(ROOT, "services", "account_manager.py"), encoding="utf-8").read()
    body = src[src.index("def _make_client("):src.index("def _direct_fallback_ok(")]
    assert "preserve_session_dc" in body
    assert body.index("_client = TelegramClient(") < body.index("preserve_session_dc")


def test_slash64_layout_unchanged():
    sub = "2a01:4f8:1c1c:abcd::/64"
    net = ipaddress.ip_network(sub)
    for aid in (1, 18, 999):
        assert eg.account_ipv6(aid, sub) == str(net[aid + 1])


def test_wide_subnet_gives_each_account_its_own_slash64():
    sub = "2a01:4f8:abcd::/48"
    net = ipaddress.ip_network(sub)
    first64 = ipaddress.ip_network("2a01:4f8:abcd::/64")
    addrs = [eg.account_ipv6(i, sub) for i in range(1, 2001)]
    assert addrs == [eg.account_ipv6(i, sub) for i in range(1, 2001)]  # детерминизм
    nets = set()
    for a in addrs:
        ip = ipaddress.ip_address(a)
        assert ip in net and ip not in first64
        nets.add(ipaddress.ip_network(f"{a}/64", strict=False))
    assert len(nets) == 2000


def test_bad_input_gives_none():
    assert eg.account_ipv6(1, "") is None
    assert eg.account_ipv6(0, "2001:db8::/64") is None
    assert eg.account_ipv6(1, "10.0.0.0/8") is None
    assert eg.account_ipv6(1, "мусор") is None


def test_probe_rejects_subnet_not_routed_here():
    # Документационный диапазон никогда не назначен хосту: bind обязан упасть.
    r = eg.probe_subnet("2001:db8:dead::/48", timeout=1)
    assert r["ok"] is False
    assert r["stage"] in ("bind", "socket")
    assert r.get("hint")


def test_probe_ok_when_bind_and_connect_work(monkeypatch):
    try:
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.bind(("::1", 0))
    except OSError:
        pytest.skip("на хосте нет IPv6 loopback")
    srv.listen(1)
    port = srv.getsockname()[1]
    t = threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True)
    t.start()
    monkeypatch.setattr(eg, "account_ipv6", lambda aid, sub: "::1")
    monkeypatch.setattr(eg, "DC_IPV6", {2: "::1"})
    monkeypatch.setattr(eg, "DC_PORT", port)
    try:
        r = eg.probe_subnet("2001:db8::/64", timeout=2)
    finally:
        srv.close()
    assert r == {"ok": True, "stage": "connect", "addr": "::1"}


def test_save_endpoint_probes_before_saving():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    body = src[src.index("async def transport_ipv6_save("):]
    body = body[:body.index("app.router.add_get")]
    assert "probe_subnet" in body
    assert body.index("probe_subnet") < body.index("db.set_ipv6_subnet")
