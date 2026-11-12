"""Гео-сеть планирует username, которые Telegram примет, и не повторяет
один username на несколько городов.

НАХОДКА. `plan_target` делал `slugify(шаблон)[:32]` и ничем это не проверял.
Шаблон без {{CITY_SLUG}} давал ОДИН username всем городам плана; короткий слаг
(«msk») и слаг с цифры Telegram не принимает; обрезка до 32 оставляла хвост
«_»; у бота исполнитель дописывал «_bot» к уже 32-символьному имени. Каждая
такая цель стоила на создании лишних запросов (отказ, перебор вариантов) —
при массовом создании каналов это ровно то, за что Telegram даёт флуд.
Фабрика (`infra_generator`) уникальность держала аллокатором, гео-сеть — нет.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): убрать вызов fit_username из build_targets ->
красные test_one_username_pattern_for_many_cities_gives_distinct_names и
test_every_planned_username_is_valid.
"""
from __future__ import annotations

from services.presence_planner import build_targets, fit_username
from services.username_engine import is_valid_username

GEO = [
    {"country": "Россия", "country_code": "RU", "city": c, "city_slug": s}
    for c, s in (("Москва", "moskva"), ("Казань", "kazan"), ("Сочи", "sochi"),
                 ("Омск", "omsk"))
]


def test_one_username_pattern_for_many_cities_gives_distinct_names():
    targets = build_targets(GEO, "channel", "Новости {{CITY_NAME}}",
                            "brand_news", [1])
    names = [t["planned_username"] for t in targets]
    assert all(names), names
    assert len(set(names)) == len(names), (
        f"один username на несколько городов: {names}")


def test_every_planned_username_is_valid():
    long_pat = "very_long_brand_name_for_city_news_{{CITY_SLUG}}"
    for asset in ("channel", "group", "bot"):
        for pat in ("{{CITY_SLUG}}", long_pat, "1{{CITY_SLUG}}", "x"):
            for t in build_targets(GEO, asset, "Новости {{CITY_NAME}}", pat, [1]):
                u = t["planned_username"]
                assert u and is_valid_username(u), (asset, pat, u)
                if asset == "bot":
                    assert u.endswith("bot"), u
                    assert len(u) <= 32


def test_fit_username_cases():
    taken: set[str] = set()
    assert fit_username("Brand_News", bot=False, taken=taken) == "brand_news"
    assert fit_username("brand_news", bot=False, taken=taken) == "brand_news_2"
    assert fit_username("msk", bot=False, taken=taken) == "tg_msk"
    assert fit_username("2gis_msk", bot=False, taken=taken) == "gis_msk"
    assert fit_username(None, bot=False, taken=taken) is None
    assert fit_username("", bot=False, taken=taken) is None
    b = fit_username("a" * 40, bot=True, taken=taken)
    assert b.endswith("_bot") and len(b) == 32 and is_valid_username(b)
    assert fit_username("shopbot", bot=True, taken=taken) == "shop_bot"
