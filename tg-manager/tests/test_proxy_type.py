"""Регрессия: определение типа прокси (add_proxy + import_proxies).

Массовый импорт прокси и проверка живости раньше отсутствовали в UI.
parse_proxy_type — единый валидатор схемы для одиночного add и массового import.
"""
from __future__ import annotations

from services.mini_app_api import parse_proxy_type


def test_socks5():
    assert parse_proxy_type("socks5://user:pass@1.2.3.4:1080") == "socks5"


def test_socks4():
    assert parse_proxy_type("socks4://1.2.3.4:1080") == "socks4"


def test_http():
    assert parse_proxy_type("http://1.2.3.4:8080") == "http"


def test_case_insensitive():
    assert parse_proxy_type("SOCKS5://host:1080") == "socks5"


def test_leading_whitespace():
    assert parse_proxy_type("  socks5://host:1080  ") == "socks5"


def test_unsupported_scheme_rejected():
    assert parse_proxy_type("https://host") is None  # https не поддержан
    assert parse_proxy_type("ftp://host") is None
    assert parse_proxy_type("host:1080") is None
    assert parse_proxy_type("") is None
    assert parse_proxy_type(None) is None
