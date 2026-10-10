"""Внутригородские районы (4-й уровень: город → район).

Реализует глубину видения «Москва → САО», «Сочи → Адлер». Развёртка совместима
со scope-моделью presence_planner: {{SCOPE}} = район, city_slug = город_район
(username уникален по районам).
"""
from __future__ import annotations

from services import geo_data as g
from services.presence_planner import plan_target


def test_city_districts_present_for_examples():
    assert len(g.city_districts("moscow")) == 12
    assert len(g.city_districts("sochi")) == 4
    # Сочи содержит Адлерский (пример из видения)
    assert any(d["slug"] == "adler" for d in g.city_districts("sochi"))
    # неизвестный город — пусто
    assert g.city_districts("kazan") == []


def test_expand_city_to_districts_scope_and_unique_slug():
    moscow = {"city": "Moscow", "city_slug": "moscow", "city_native": "Москва",
              "region": "Moscow", "country_code": "ru"}
    nodes = g.expand_city_to_districts(moscow)
    assert len(nodes) == 12
    slugs = {n["city_slug"] for n in nodes}
    assert len(slugs) == 12, "city_slug районов должны быть уникальны (для username)"
    assert all(n["city_slug"].startswith("moscow_") for n in nodes)
    assert all(n.get("scope") for n in nodes), "scope (имя района) должен быть проставлен"


def test_expand_without_districts_returns_city():
    kazan = {"city": "Kazan", "city_slug": "kazan"}
    assert g.expand_city_to_districts(kazan) == [kazan]


def test_district_node_renders_via_scope():
    sochi = {"city": "Sochi", "city_slug": "sochi", "city_native": "Сочи",
             "region": "Krasnodar Krai", "country_code": "ru"}
    nodes = g.expand_city_to_districts(sochi)
    adler = next(n for n in nodes if n["district_slug"] == "adler")
    name, uname = plan_target(adler, 1, "Новости {{SCOPE}}", "{{CITY_SLUG}}_news")
    assert "Адлер" in name, name
    assert uname.startswith("sochi_adler"), uname
    # username районов различаются
    unames = {plan_target(n, 1, "X", "{{CITY_SLUG}}")[1] for n in nodes}
    assert len(unames) == len(nodes)


def test_all_district_slugs_ascii_lower():
    for slug, districts in g.CITY_DISTRICTS.items():
        for d in districts:
            assert d["slug"] == d["slug"].lower()
            assert all(ch.isascii() for ch in d["slug"]), d["slug"]
