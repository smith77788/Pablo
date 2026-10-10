"""Регресс: гео-нормализация к ISO2 + бэкфилл-миграция.

Первопричина бага: детект прокси писал geo_country полным именем («Ukraine»),
а матч-сайты сравнивают с ISO2 («UA») → гео-подбор не работал. Проверяем
нормализатор, флаги, консистентность бэкфилл-миграции и проводку детекта.
"""
from __future__ import annotations

import os

from services.geo_normalize import to_iso2, flag_emoji, name_map

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_full_names_to_iso2():
    assert to_iso2("Ukraine") == "UA"
    assert to_iso2("Germany") == "DE"
    assert to_iso2("United States") == "US"
    assert to_iso2("Russia") == "RU"
    assert to_iso2("United Kingdom") == "GB"
    # альтернативные написания ip-api
    assert to_iso2("Czechia") == "CZ"
    assert to_iso2("South Korea") == "KR"
    assert to_iso2("Türkiye") == "TR"


def test_codes_passthrough_and_unknown():
    assert to_iso2("ua") == "UA"
    assert to_iso2("DE") == "DE"
    assert to_iso2("") is None
    assert to_iso2(None) is None
    assert to_iso2("Neverland") is None


def test_flag_emoji():
    assert flag_emoji("UA") == "\U0001F1FA\U0001F1E6"
    assert flag_emoji("de") == "\U0001F1E9\U0001F1EA"
    assert flag_emoji("X") == ""
    assert flag_emoji(None) == ""


def test_backfill_migration_consistent_with_map():
    """Каждая пара имя→ISO2 из name_map присутствует в бэкфилл-миграции
    (единый источник — без дрейфа)."""
    sql = _read("schema_v190_geo_country_iso2.sql")
    # идемпотентность и безопасность
    assert "char_length(p.geo_country) > 2" in sql
    assert "UPDATE user_proxies" in sql and "geo_country = m.iso2" in sql
    m = name_map()
    checked = 0
    for name, code in m.items():
        if len(name) == 2 and name.isalpha():
            continue  # коды не бэкфиллим
        esc = name.replace("'", "''")  # SQL-экранирование апострофа
        assert f"('{esc}','{code}')" in sql, f"нет пары в миграции: {name}->{code}"
        checked += 1
    assert checked > 100  # покрытие реальное, не пустое


def test_detection_writes_iso2():
    src = _read("bot/handlers/proxy_manager.py")
    # ip-api запрашивает countryCode и нормализует к ISO2
    assert "countryCode" in src
    assert "to_iso2(" in src


def test_match_sites_expect_iso2_unchanged():
    # матч-сайты уже сравнивают с ISO2 — фикс их не ломает
    am = _read("services/account_manager.py")
    assert "UPPER(p.geo_country) = UPPER($2)" in am
