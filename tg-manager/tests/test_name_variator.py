"""Генерация имён/юзернеймов перестановкой ключей (SEO-массив создания ресурсов).

Владелец занимает много мест в поиске, создавая ресурсы с одними ключами, но
по-разному: меняя слова местами и добавляя буквы. Проверяем, что движок:
даёт РАЗНЫЕ валидные юзернеймы, начинает с перестановок, добавляет буквы, уважает
регистронезависимость Telegram, исключает занятые и умеет суффикс «bot».
"""
from __future__ import annotations

from services import name_variator as V


def test_split_keywords_variants():
    assert V.split_keywords("@Dostavka_Moskva") == ["Dostavka", "Moskva"]
    assert V.split_keywords("Dostavka Moskva") == ["Dostavka", "Moskva"]
    assert V.split_keywords("a-b+c") == ["a", "b", "c"]
    assert V.split_keywords("") == []


def test_valid_username_rules():
    assert V.valid_username("dostavka_moskva")
    assert not V.valid_username("ab")                 # слишком коротко
    assert not V.valid_username("1dostavka")          # начинается с цифры
    assert not V.valid_username("dostavka_")          # хвостовой _
    assert not V.valid_username("dost__avka")         # сдвоенный __
    assert not V.valid_username("плохой_юзер")        # не латиница
    assert V.valid_username("delivery_bot", require_suffix="bot")
    assert not V.valid_username("delivery_shop", require_suffix="bot")


def test_first_variants_are_permutations():
    got = V.generate_usernames("Dostavka_Moskva", 2)
    assert set(got) == {"dostavka_moskva", "moskva_dostavka"}


def test_usernames_are_distinct_and_valid():
    got = V.generate_usernames("Dostavka_Moskva", 30)
    assert len(got) == 30
    assert len(set(got)) == 30                        # все разные
    assert all(V.valid_username(u) for u in got)
    # первые два — перестановки, дальше — с добавленными буквами
    assert got[0] == "dostavka_moskva"
    assert any(u not in ("dostavka_moskva", "moskva_dostavka") for u in got)


def test_case_insensitive_dedup_against_taken():
    # заняты в РАЗНОМ регистре — не должны выдаться снова
    got = V.generate_usernames(
        "Dostavka_Moskva", 5, taken=["DOSTAVKA_MOSKVA", "@Moskva_Dostavka"])
    low = {g.lower() for g in got}
    assert "dostavka_moskva" not in low
    assert "moskva_dostavka" not in low
    assert len(got) == 5


def test_added_letters_keep_keywords_present():
    # добавляем буквы к слову — ключ остаётся распознаваемым (Dostavka… / …Moskva…)
    got = V.generate_usernames("Dostavka_Moskva", 20)
    grown = [u for u in got if u not in ("dostavka_moskva", "moskva_dostavka")]
    assert grown, "должны появиться варианты с добавленными буквами"
    for u in grown:
        assert "dostavka" in u or "moskva" in u


def test_bot_suffix_enforced():
    got = V.generate_usernames("Dostavka_Moskva", 6, require_suffix="bot")
    assert len(got) == 6
    assert all(u.endswith("bot") for u in got)
    assert all(V.valid_username(u, require_suffix="bot") for u in got)


def test_titles_permute_and_keep_case():
    got = V.generate_titles("Доставка Москва", 2)
    assert set(got) == {"Доставка Москва", "Москва Доставка"}


def test_titles_enough_even_beyond_permutations():
    got = V.generate_titles("Доставка Москва", 10)
    assert len(got) == 10
    assert got[0] == "Доставка Москва"


def test_single_keyword_titles_and_usernames():
    ts = V.generate_titles("Доставка", 3)
    assert len(ts) == 3 and ts[0] == "Доставка"
    us = V.generate_usernames("dostavka", 3)
    assert len(us) == 3 and all(V.valid_username(u) for u in us)


def test_deterministic():
    a = V.generate_usernames("Dostavka_Moskva", 15, seed=7)
    b = V.generate_usernames("Dostavka_Moskva", 15, seed=7)
    assert a == b
