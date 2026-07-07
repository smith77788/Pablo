"""Регрессия: проверка уникальности IP (изоляция) — раньше validate_ip_diversity
была мёртвой (0 вызовов). Теперь подключена через audit_proxy_isolation/эндпоинт.

Логика должна: группировать аккаунты по IP прокси (с расшифровкой зашифрованного
proxy_url), ловить общий IP (риск бана), считать datacenter-IP.
"""
from __future__ import annotations

from services.proxy_selector import validate_ip_diversity
from services.token_vault import encrypt_token


def test_shared_ip_detected_across_encrypted_proxies():
    # два аккаунта на ОДНОМ IP (разный шифротекст!), один — на своём, один — без прокси
    accts = [
        {"id": 1, "proxy_url": encrypt_token("socks5://u:p@1.1.1.1:1080")},
        {"id": 2, "proxy_url": encrypt_token("socks5://x:y@1.1.1.1:1080")},  # тот же IP
        {"id": 3, "proxy_url": encrypt_token("socks5://a:b@2.2.2.2:1080")},
        {"id": 4, "proxy_url": ""},  # без прокси
    ]
    r = validate_ip_diversity(accts, max_per_ip=1)
    assert not r["valid"]  # есть нарушение (1.1.1.1 у двоих)
    assert set(r["ip_usage"]["1.1.1.1"]) == {1, 2}
    assert r["ip_usage"]["2.2.2.2"] == [3]
    # аккаунт без прокси не создаёт IP-группу
    assert "" not in r["ip_usage"]


def test_all_unique_ips_valid():
    accts = [
        {"id": 1, "proxy_url": encrypt_token("socks5://u:p@1.1.1.1:1080")},
        {"id": 2, "proxy_url": encrypt_token("socks5://u:p@2.2.2.2:1080")},
    ]
    r = validate_ip_diversity(accts, max_per_ip=1)
    assert r["valid"]
    assert all(len(ids) == 1 for ids in r["ip_usage"].values())


def test_audit_endpoint_wired():
    # раньше validate_ip_diversity была мёртвой — guard, что теперь подключена
    import inspect
    from services import proxy_selector, mini_app_api

    assert "validate_ip_diversity" in inspect.getsource(proxy_selector.audit_proxy_isolation)
    assert "isolation_check" in inspect.getsource(mini_app_api)
