"""Регресс: транспорт-хост аккаунта показывается чисто и единообразно.

`transport["host"]` (мини-апп: «выходной IP/хост · уникальный IP 1:1») брался как
`decrypt(proxy_url).split('@')[-1]`. Для прокси БЕЗ креденшелов (нет '@') это
возвращало весь URL со схемой и портом (`socks5://1.2.3.4:1080`) вместо чистого
host — рассинхрон с форматом с креденшелами и шум в поле «уникальный IP». Хелпер
`_proxy_display_host` даёт чистый host единообразно, без креденшелов.
"""
from __future__ import annotations

from services.mini_app_api import _proxy_display_host


def test_display_host_credentialless_is_clean():
    # раньше давало 'socks5://1.2.3.4:1080' — весь URL
    assert _proxy_display_host("socks5://1.2.3.4:1080") == "1.2.3.4"
    assert _proxy_display_host("5.6.7.8:8080") == "5.6.7.8"


def test_display_host_strips_credentials():
    # креденшелы никогда не показываем
    assert _proxy_display_host("socks5://user:pass@1.2.3.4:1080") == "1.2.3.4"


def test_display_host_ipv6_and_domain_and_empty():
    assert _proxy_display_host("socks5://[2001:db8::1]:1080") == "2001:db8::1"
    assert _proxy_display_host("proxy.example.com:1080") == "proxy.example.com"
    assert _proxy_display_host("") == ""
    assert _proxy_display_host(None) == ""
