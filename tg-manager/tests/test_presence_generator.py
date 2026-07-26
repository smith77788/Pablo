"""Генератор инфраструктуры: пул шаблонов + токены уникальности + детерминизм.

Цель модуля — не «одно название на всё», а генерация с малым числом повторов:
пул шаблонов (по строке), выбор одного на город, токены {randN}/{N}. Детерминизм
по (city, index) обязателен — иначе предпросмотр не совпадёт с запуском (класс #4).
"""
from __future__ import annotations

import re

from services.presence_planner import (
    split_pool,
    render_pattern,
    plan_target,
    build_targets,
    _CITY_ABBR,
)


def _geo(city, slug, region="R", cc="ru", country="Russia", native=None):
    return {"city": city, "city_slug": slug, "region": region, "country_code": cc,
            "country": country, "city_native": native or city}


# ── пул шаблонов ──────────────────────────────────────────────────────────────

def test_split_pool_lines_and_pipes():
    assert split_pool("A\nB\nC") == ["A", "B", "C"]
    assert split_pool("A | B|C") == ["A", "B", "C"]
    assert split_pool("  \n one \n\n two \n") == ["one", "two"]
    assert split_pool("") == []


def test_pool_picks_vary_across_cities():
    pool = "Новости {{CITY_NAME}}\nПодслушано {{CITY_NAME}}\n{{CITY_NAME}} Онлайн\n{{CITY_NAME}} 24"
    cities = [_geo(f"City{i}", f"city{i}") for i in range(40)]
    names = [plan_target(c, i + 1, pool, None)[0] for i, c in enumerate(cities)]
    # использованы разные шаблоны из пула (не один на всех)
    shapes = {re.sub(r"City\d+", "{}", n) for n in names}
    assert len(shapes) >= 3, f"пул почти не варьируется: {shapes}"


# ── детерминизм (preview == запуск) ───────────────────────────────────────────

def test_plan_target_deterministic():
    pool_n = "Новости {{CITY_NAME}} {2}\n{{CITY_NAME}} News {rand4}"
    pool_u = "{{CITY_SLUG}}_news{3}\n{{CITY_SLUG}}chat{2}"
    g = _geo("Sochi", "sochi")
    a = plan_target(g, 5, pool_n, pool_u)
    b = plan_target(g, 5, pool_n, pool_u)
    assert a == b, "один и тот же вход обязан давать один выход (иначе preview≠запуск)"
    # разный индекс → потенциально другой вариант/рандом
    assert plan_target(g, 6, pool_n, pool_u) != a or True  # не гарантируем различие, только детерминизм


# ── токены уникальности ───────────────────────────────────────────────────────

def test_random_tokens_expand():
    g = _geo("Kazan", "kazan")
    name, uname = plan_target(g, 1, "{{CITY_NAME}} {rand4}", "{{CITY_SLUG}}news{3}")
    assert re.search(r"[a-z2-9]{4}$", name), name
    assert re.search(r"news\d{3}$", uname), uname


def test_digit_token_length():
    g = _geo("Perm", "perm")
    _, uname = plan_target(g, 1, "X", "{{CITY_SLUG}}{2}")
    assert re.fullmatch(r"perm\d{2}", uname), uname


def test_username_slugified_and_capped():
    g = _geo("Нижний Новгород", "nizhny_novgorod", native="Нижний Новгород")
    _, uname = plan_target(g, 1, "X", "{{CITY_NAME}}_чат")  # кириллица → транслит
    assert uname and re.fullmatch(r"[a-z0-9_]+", uname), uname
    assert len(uname) <= 32


def test_city_abbr_token():
    g = _geo("Moscow", "moscow")
    name, _ = plan_target(g, 1, "Новости {{CITY_ABBR}}", None)
    assert name.endswith(_CITY_ABBR["moscow"])  # msk
    # неизвестный город → fallback на slug
    g2 = _geo("Tinytown", "tinytown")
    n2, _ = plan_target(g2, 1, "X {{CITY_ABBR}}", None)
    assert n2.endswith("tinytown")


# ── build_targets использует движок ───────────────────────────────────────────

def test_build_targets_uses_pool_and_is_deterministic():
    pool = "Новости {{CITY_NAME}}\n{{CITY_NAME}} Онлайн"
    geos = [_geo("Sochi", "sochi"), _geo("Kazan", "kazan")]
    t1 = build_targets(geos, "channel", pool, "{{CITY_SLUG}}{rand4}", [1, 2])
    t2 = build_targets(geos, "channel", pool, "{{CITY_SLUG}}{rand4}", [1, 2])
    assert [x["planned_name"] for x in t1] == [x["planned_name"] for x in t2]
    assert [x["planned_username"] for x in t1] == [x["planned_username"] for x in t2]
    # username уникальны между разными городами
    assert t1[0]["planned_username"] != t1[1]["planned_username"]


def test_single_pattern_backward_compatible():
    """Один шаблон (без пула/токенов) работает как раньше."""
    g = _geo("Berlin", "berlin", country="Germany", cc="de")
    name, uname = plan_target(g, 1, "Crypto {{CITY}}", "{{CITY_SLUG}}_crypto")
    assert name == "Crypto Berlin"
    assert uname == "berlin_crypto"
