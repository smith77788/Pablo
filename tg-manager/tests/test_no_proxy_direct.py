"""Без прокси → прямой выход с реального host-IP (а не бесплатный SOCKS-пул).

Корень жалобы «свежий флот без прокси не стартует»: безпроксёвый аккаунт уходил в
публичный free-pool, а тот нестабилен → скачок IP между логином и операцией →
AUTH_KEY_DUPLICATED / «не ответил». Теперь по умолчанию такой аккаунт идёт ПРЯМО
(один и тот же host-IP везде); free-pool включается только USE_FREE_POOL=1.
telethon застаблен в conftest, поэтому _make_client возвращает лёгкую заглушку с
проставленным _infragram_transport.
"""
from __future__ import annotations

from services import account_manager as am


def _no_transport_env(monkeypatch):
    # Ни IPv6, ни CF-relay не сконфигурированы (как у оператора без прокси).
    monkeypatch.setattr(am, "_IPV6_SUBNET", "")
    monkeypatch.setattr(am, "CF_RELAY_URL", "")


def test_no_proxy_goes_direct_by_default(monkeypatch):
    _no_transport_env(monkeypatch)
    monkeypatch.setattr(am, "_USE_FREE_POOL", False)
    client = am._make_client("", {"id": 1, "phone": "+70000000001"})
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_no_proxy_login_also_direct(monkeypatch):
    # На логине account_id ещё нет — важно, чтобы транспорт был тот же (direct),
    # что и в операции, иначе логин и операция идут с разных IP.
    _no_transport_env(monkeypatch)
    monkeypatch.setattr(am, "_USE_FREE_POOL", False)
    client = am._make_client("", {"phone": "+70000000002"})  # без id (как логин)
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_free_pool_used_only_when_enabled(monkeypatch):
    _no_transport_env(monkeypatch)
    monkeypatch.setattr(am, "_USE_FREE_POOL", True)
    monkeypatch.setattr(am, "_get_pool_proxy_url", lambda key=None: "socks5://1.2.3.4:1080")
    monkeypatch.setattr(am, "_parse_proxy",
                        lambda url: (2, "1.2.3.4", 1080, True, None, None) if url else None)
    client = am._make_client("", {"id": 3, "phone": "+70000000003"})
    assert getattr(client, "_infragram_transport", None) == "pool"


def test_bound_proxy_untouched(monkeypatch):
    # Аккаунт с назначенным прокси всегда идёт через него — фикс не влияет.
    _no_transport_env(monkeypatch)
    monkeypatch.setattr(am, "_USE_FREE_POOL", False)
    monkeypatch.setattr(am, "_parse_proxy",
                        lambda url: (2, "9.9.9.9", 1080, True, None, None) if url else None)
    client = am._make_client("", {"id": 4, "proxy_url": "socks5://9.9.9.9:1080"})
    assert getattr(client, "_infragram_transport", None) == "bound"
