"""Регресс: извлечение IP прокси и изоляция ловят прокси БЕЗ креденшелов.

Ядро-дифференциатор — прокси-изоляция 1:1 на аккаунт. `extract_ip_from_proxy` брал
IP только из формата `scheme://user:pass@host:port` (regex требовал '@'). Прокси без
auth (`socks5://1.2.3.4:1080`), голый `host:port`, http без auth и IPv6 → None →
аккаунт молча выпадал из `validate_ip_diversity` → ДВА аккаунта на одном IP НЕ
флагались, `isolation_ok` лгал «в порядке». Это ложно-негативная изоляция —
координационная сигнатура, которая дороже обычной фичи.
"""
from __future__ import annotations

from services.proxy_selector import extract_ip_from_proxy, validate_ip_diversity


def test_extract_ip_handles_credentialless_and_bare():
    assert extract_ip_from_proxy("socks5://user:pass@1.2.3.4:1080") == "1.2.3.4"
    # Раньше давали None (баг):
    assert extract_ip_from_proxy("socks5://1.2.3.4:1080") == "1.2.3.4"
    assert extract_ip_from_proxy("http://5.6.7.8:8080") == "5.6.7.8"
    assert extract_ip_from_proxy("1.2.3.4:1080") == "1.2.3.4"


def test_extract_ip_handles_ipv6_and_password_with_at():
    assert extract_ip_from_proxy("socks5://[2001:db8::1]:1080") == "2001:db8::1"
    # пароль с '@' не должен сбивать (host — после последнего '@')
    assert extract_ip_from_proxy("socks5://user:p@ss@1.2.3.4:1080") == "1.2.3.4"


def test_domain_proxy_returns_none():
    # домен без резолва к IP не сгруппировать — отсеиваем (как и раньше)
    assert extract_ip_from_proxy("proxy.example.com:1080") is None
    assert extract_ip_from_proxy("") is None
    assert extract_ip_from_proxy(None) is None


def test_shared_credentialless_ip_is_flagged():
    # Раньше: два аккаунта на одном creds-less IP → valid=True, ip_usage={} (тупо не видели).
    accs = [
        {"id": 1, "proxy_url": "socks5://9.9.9.9:1080"},
        {"id": 2, "proxy_url": "socks5://9.9.9.9:1080"},
    ]
    r = validate_ip_diversity(accs, max_per_ip=1)
    assert r["valid"] is False, "общий IP без креденшелов обязан флагаться"
    assert r["ip_usage"].get("9.9.9.9") == [1, 2]
    assert len(r["warnings"]) == 1
