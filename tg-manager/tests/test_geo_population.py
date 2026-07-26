"""Гео: население городов + фильтр «города с населением > N».

Разблокирует сценарий видения «все города с населением >50 000». Таблица
CITY_POPULATION (slug→население) отдельная — добавление не трогает сотни
существующих записей городов.
"""
from __future__ import annotations

from services import geo_data as g


def test_city_population_lookup():
    assert g.city_population("moscow") > 10_000_000
    assert g.city_population("sochi") and g.city_population("sochi") < 1_000_000
    assert g.city_population("MOSCOW") == g.city_population("moscow")  # регистр
    assert g.city_population("unknown_town") is None


def test_enrich_with_population_adds_field_without_mutating():
    src = [{"city": "Kazan", "city_slug": "kazan"}]
    out = g.enrich_with_population(src)
    assert out[0]["population"] == g.city_population("kazan")
    assert "population" not in src[0], "исходный список не должен мутироваться"


def test_filter_by_population_threshold():
    cities = [
        {"city": "Moscow", "city_slug": "moscow"},
        {"city": "Sochi", "city_slug": "sochi"},
        {"city": "Nowhere", "city_slug": "nowhere"},  # нет данных
    ]
    over_1m = {c["city"] for c in g.filter_by_population(cities, 1_000_000)}
    assert over_1m == {"Moscow"}
    over_50k = {c["city"] for c in g.filter_by_population(cities, 50_000)}
    assert over_50k == {"Moscow", "Sochi"}  # Nowhere без данных — исключён


def test_filter_excludes_unknown_population():
    """Города без данных о населении не проходят порог (честность, не случайность)."""
    cities = [{"city": "X", "city_slug": "no_such_slug"}]
    assert g.filter_by_population(cities, 10_000) == []


def test_filter_min_pop_zero_returns_all():
    cities = [{"city": "A", "city_slug": "no_data"}]
    assert g.filter_by_population(cities, 0) == cities


def test_preset_options_carry_population_and_district():
    opts = g.preset_city_options("russia")
    moscow = next(o for o in opts if o["city"] == "Moscow")
    assert moscow["population"] == g.city_population("moscow")
    assert moscow["federal_district"] == "Центральный ФО"
    # обратная совместимость: старые поля на месте
    assert {"city", "city_native", "city_slug", "country"} <= set(opts[0])


def test_preset_options_min_population_filter():
    big = g.preset_city_options("russia", min_population=1_000_000)
    assert big and all(o["population"] >= 1_000_000 for o in big)
    # без фильтра городов больше
    assert len(big) < len(g.preset_city_options("russia"))


def test_population_keys_match_known_city_slugs():
    """Ключи таблицы населения — валидные slug'и (нижний регистр, без пробелов)."""
    for slug in g.CITY_POPULATION:
        assert slug == slug.lower().strip()
        assert " " not in slug
        assert g.CITY_POPULATION[slug] > 0
