"""Регрессия: выбор конкретных городов из гео-пресета (Global Presence).

Чистые хелперы geo_data + инвариант «пустой выбор = весь пресет» (обратная
совместимость с прежним поведением, когда выбирался пресет целиком).
"""
from __future__ import annotations

import os

from services.geo_data import GEO_PRESETS, preset_city_options, filter_preset_cities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KEY = next(iter(GEO_PRESETS))


def test_options_shape():
    opts = preset_city_options(_KEY)
    assert opts
    for o in opts:
        assert set(("city", "city_native", "city_slug", "country")) <= set(o.keys())


def test_unknown_preset_empty():
    assert preset_city_options("does_not_exist") == []
    assert filter_preset_cities("does_not_exist", ["x"]) == []


def test_empty_selection_returns_whole_preset():
    full = GEO_PRESETS[_KEY]["cities"]
    assert len(filter_preset_cities(_KEY, [])) == len(full)
    assert len(filter_preset_cities(_KEY, None)) == len(full)
    # список из пустых строк — тоже «все»
    assert len(filter_preset_cities(_KEY, ["", "  "])) == len(full)


def test_subset_by_slug_and_name_case_insensitive():
    opts = preset_city_options(_KEY)
    slug = opts[0]["city_slug"]
    name = opts[1]["city"]
    sub = filter_preset_cities(_KEY, [slug, name.upper()])
    got_slugs = {c["city_slug"] for c in sub}
    assert opts[0]["city_slug"] in got_slugs
    assert opts[1]["city_slug"] in got_slugs
    assert len(sub) == 2


def test_endpoint_and_create_wired():
    with open(os.path.join(ROOT, "services/mini_app_api.py"), encoding="utf-8") as f:
        src = f.read()
    # эндпоинт умеет отдавать города пресета
    assert 'request.rel_url.query.get("cities")' in src
    assert "preset_city_options(want)" in src
    # create принимает подмножество и фильтрует
    assert 'body.get("preset_cities")' in src
    assert "filter_preset_cities(geo_preset, preset_cities)" in src


def test_ui_sends_preset_cities():
    with open(os.path.join(ROOT, "mini_app/index.html"), encoding="utf-8") as f:
        ui = f.read()
    for fn in ("gpLoadPresetCities", "gpToggleAllCities", "gpUpdateCityCount"):
        assert fn in ui
    assert "payload.preset_cities = chosen" in ui
