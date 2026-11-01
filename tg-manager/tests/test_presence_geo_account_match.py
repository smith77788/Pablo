"""Гео-сеть привязывает цель под страну X к аккаунту из страны X.

Раньше build_targets раздавал аккаунты round-robin по индексу, игнорируя страну
цели: бот/канал под DE мог создаться с аккаунта, чей прокси в RU. Теперь
assign_accounts_by_geo предпочитает аккаунт из страны цели (round-robin внутри
страны), а при отсутствии такого — прежний глобальный round-robin.
"""
from __future__ import annotations

from services.presence_planner import assign_accounts_by_geo, build_targets


def test_targets_get_account_from_their_country():
    # аккаунты: 1→RU, 2→DE; цели: RU, DE, DE
    out = assign_accounts_by_geo(
        ["RU", "DE", "DE"], [1, 2], {1: "RU", 2: "DE"})
    assert out == [1, 2, 2]  # каждая цель — аккаунт своей страны


def test_round_robin_within_a_country():
    # три аккаунта DE на две цели DE — распределяются, не грузят один
    out = assign_accounts_by_geo(
        ["DE", "DE"], [5, 6, 7], {5: "DE", 6: "DE", 7: "DE"})
    assert out == [5, 6]


def test_falls_back_to_global_rr_when_no_match():
    # цель FR, аккаунтов FR нет → прежний глобальный round-robin
    out = assign_accounts_by_geo(
        ["FR", "FR", "FR"], [1, 2], {1: "RU", 2: "DE"})
    assert out == [1, 2, 1]


def test_case_insensitive_and_missing_geo():
    out = assign_accounts_by_geo(
        ["de", None, ""], [9], {9: "De"})
    # 'de'==geo 'De' → 9; цель без страны → глобальный rr → 9; пустая → 9
    assert out == [9, 9, 9]


def test_empty_account_geo_is_old_behavior():
    # без гео аккаунтов — ровно прежний i % n
    out = assign_accounts_by_geo(["RU", "DE", "US", "FR"], [1, 2], None)
    assert out == [1, 2, 1, 2]


def test_no_accounts_gives_none():
    assert assign_accounts_by_geo(["RU", "DE"], [], {}) == [None, None]


def test_build_targets_uses_geo_account():
    geo_list = [
        {"country": "Германия", "country_code": "DE", "city": "Berlin"},
        {"country": "Россия", "country_code": "RU", "city": "Москва"},
    ]
    targets = build_targets(
        geo_list, "bot", "Bot {{CITY}}", None, [10, 20],
        account_geo={10: "RU", 20: "DE"})
    by_city = {t["city"]: t["selected_account_id"] for t in targets}
    assert by_city["Berlin"] == 20   # DE-цель → DE-аккаунт
    assert by_city["Москва"] == 10   # RU-цель → RU-аккаунт


def test_build_targets_without_geo_is_backward_compatible():
    geo_list = [{"country_code": "DE", "city": "A"}, {"country_code": "RU", "city": "B"}]
    targets = build_targets(geo_list, "bot", "X", None, [1, 2])  # account_geo по умолчанию None
    assert [t["selected_account_id"] for t in targets] == [1, 2]
