"""Без прокси → прямой выход с реального host-IP (free-pool УДАЛЁН).

Политика транспорта: прокси задаёт пользователь; если прокси/релей/IPv6 не заданы —
стабильный ПРЯМОЙ host-IP (один и тот же на логине и в операциях). Бесплатный
публичный SOCKS-пул удалён полностью — он был нестабилен (скачок IP между логином
и операцией → AUTH_KEY_DUPLICATED) и блокировал работу флота.

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
    client = am._make_client("", {"id": 1, "phone": "+70000000001"})
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_no_proxy_login_also_direct(monkeypatch):
    # На логине account_id ещё нет — транспорт тот же (direct), что и в операции,
    # иначе логин и операция шли бы с разных IP.
    _no_transport_env(monkeypatch)
    client = am._make_client("", {"phone": "+70000000002"})  # без id (как логин)
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_free_pool_removed_no_pool_transport(monkeypatch):
    # free-pool удалён: даже без прокси/релея/IPv6 транспорт всегда 'direct',
    # публичный пул НИКОГДА не используется. Заглушка-функция — no-op.
    _no_transport_env(monkeypatch)
    am.set_pool_proxy_cache(["socks5://1.2.3.4:1080"])   # no-op, ни на что не влияет
    client = am._make_client("", {"id": 3, "phone": "+70000000003"})
    assert getattr(client, "_infragram_transport", None) == "direct"
    assert not hasattr(am, "_get_pool_proxy_url")        # функция удалена


def test_bound_proxy_untouched(monkeypatch):
    # Аккаунт с назначенным прокси всегда идёт через него.
    _no_transport_env(monkeypatch)
    monkeypatch.setattr(am, "_parse_proxy",
                        lambda url: (2, "9.9.9.9", 1080, True, None, None) if url else None)
    client = am._make_client("", {"id": 4, "proxy_url": "socks5://9.9.9.9:1080"})
    assert getattr(client, "_infragram_transport", None) == "bound"
