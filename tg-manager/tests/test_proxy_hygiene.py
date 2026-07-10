"""Регрессия: proxy hygiene — безопасное удаление/вычистка/экспорт прокси.

Критично (класс багов «изоляция», CLAUDE.md): нельзя удалить назначенный прокси —
FK ON DELETE SET NULL обнулит proxy_id аккаунта → прямое подключение → AUTH_KEY_DUPLICATED.
Чистые решающие функции + проводка guard'а delete_proxy / cleanup / export.
"""
from __future__ import annotations

import os

from services.proxy_hygiene import (
    proxy_is_dead, can_delete_safely, is_dead_removable, mask_proxy_url,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_can_delete_only_unassigned():
    assert can_delete_safely(0) is True
    assert can_delete_safely(1) is False
    assert can_delete_safely(5) is False
    assert can_delete_safely(None) is True   # 0 назначений
    assert can_delete_safely("bad") is False  # неизвестно → не удаляем


def test_proxy_is_dead_semantics():
    assert proxy_is_dead(is_active=False, is_alive=True) is True   # деактивирован
    assert proxy_is_dead(is_active=True, is_alive=False) is True   # проба-мёртв
    assert proxy_is_dead(is_active=True, is_alive=True) is False   # живой
    # НЕПРОВЕРЕН (is_alive NULL) — НЕ мёртвый: неизвестность ≠ смерть
    assert proxy_is_dead(is_active=True, is_alive=None) is False


def test_dead_removable_requires_unassigned_and_probed_dead():
    # удаляем только: не назначен И подтверждён мёртвым пробой
    assert is_dead_removable(0, True, False) is True
    # назначен — НИКОГДА (даже если мёртв) → защита изоляции
    assert is_dead_removable(2, True, False) is False
    # непроверенный (NULL) не считается мёртвым — не удаляем
    assert is_dead_removable(0, True, None) is False
    # живой — не удаляем
    assert is_dead_removable(0, True, True) is False


def test_mask_hides_credentials_keeps_host():
    assert mask_proxy_url("socks5://user:pass@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"
    assert mask_proxy_url("http://log:pw@host.tld:8080") == "http://***@host.tld:8080"
    # без кредов — маскировать нечего
    assert mask_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert mask_proxy_url("") == "" and mask_proxy_url(None) == ""


def test_endpoints_routes_and_ui_wired():
    api = _read("services/mini_app_api.py")
    # delete_proxy теперь с guard'ом изоляции (проверка назначения + 409)
    seg = api[api.index("async def delete_proxy"):api.index("async def proxy_cleanup_dead")]
    assert "proxy_hygiene.can_delete_safely" in seg and "AUTH_KEY_DUPLICATED" in seg
    assert "async def proxy_cleanup_dead" in api and "async def proxy_export" in api
    # cleanup удаляет только НЕназначенные мёртвые (NOT EXISTS по tg_accounts)
    cseg = api[api.index("async def proxy_cleanup_dead"):api.index("async def proxy_export")]
    assert "is_alive IS FALSE" in cseg and "NOT EXISTS" in cseg
    assert 'add_post("/api/miniapp/proxy/cleanup_dead", proxy_cleanup_dead)' in api
    assert 'add_get("/api/miniapp/proxy/export", proxy_export)' in api
    ui = _read("mini_app/index.html")
    assert "exportProxies" in ui and "cleanupDeadProxies" in ui
    assert "/api/miniapp/proxy/cleanup_dead" in ui and "/api/miniapp/proxy/export" in ui


def test_proxy_stats_masks_and_enriches():
    api = _read("services/mini_app_api.py")
    seg = api[api.index("async def proxy_stats"):api.index("async def ecosystem_recommendations")]
    # НЕ отдаём сырой ENC:-шифротекст в UI: расшифровка + маскировка
    assert "decrypt_token" in seg and "proxy_hygiene.mask_proxy_url" in seg
    assert 'r["proxy_url"][:30]' not in seg  # старый баг (показ шифротекста) устранён
    # durable-здоровье и назначение обогащены
    assert "is_alive" in seg and "last_check" in seg and "assigned" in seg
    ui = _read("mini_app/index.html")
    assert "toggleProxyStats" in ui and "/api/miniapp/proxy_stats" in ui
