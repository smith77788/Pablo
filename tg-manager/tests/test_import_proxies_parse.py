"""Массовый импорт прокси: разбор/валидация вставленного списка.

Проверяем чистую _collect_valid_proxies (без БД): валидные схемы проходят,
невалидные/loopback/переросшие лимит длины — в skipped, дубли (по
proxy_fingerprint) схлопываются и НЕ считаются пропущенными, лимит 500 держит,
разделители — перевод строки, запятая, точка с запятой.
"""
from __future__ import annotations

from services.mini_app_api import _collect_valid_proxies


def test_valid_dedup_and_skips():
    raw = (
        "socks5://1.1.1.1:1080\n"
        "\n"                          # пустая — не пропуск
        "socks5://1.1.1.1:1080\n"      # дубль — схлопнуть, не пропуск
        "ftp://x\n"                    # неподдержанная схема — skip
        "http://127.0.0.1:8080\n"      # loopback — skip
        "http://2.2.2.2:3128"
    )
    purls, skipped = _collect_valid_proxies(raw)
    assert purls == ["socks5://1.1.1.1:1080", "http://2.2.2.2:3128"]
    assert skipped == 2  # ftp + loopback; пустая и дубль НЕ считаются


def test_separators_and_overlong():
    long = "http://" + "a" * 600  # > 500 символов → skip
    raw = f"socks4://3.3.3.3:1080;{long},socks5://4.4.4.4:1080"
    purls, skipped = _collect_valid_proxies(raw)
    assert purls == ["socks4://3.3.3.3:1080", "socks5://4.4.4.4:1080"]
    assert skipped == 1


def test_cap_500():
    raw = "\n".join(f"socks5://1.2.3.{i}:1080" for i in range(600))
    purls, skipped = _collect_valid_proxies(raw)
    assert len(purls) == 500
    assert skipped == 0


def test_loopback_substring_filter():
    # адрес, оканчивающийся на 0.0.0.0, отсекается тем же фильтром (сохранённое
    # поведение) — сеть-адрес всё равно не рабочий прокси-хост
    purls, skipped = _collect_valid_proxies("socks5://10.0.0.0:1080")
    assert purls == [] and skipped == 1


def test_empty_inputs():
    assert _collect_valid_proxies("") == ([], 0)
    assert _collect_valid_proxies(None) == ([], 0)  # type: ignore[arg-type]
