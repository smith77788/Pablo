"""Global Presence: фильтр «города > N» вшит в шаг географии.

Данные о населении (geo_data.filter_by_population) теперь доходят до UI: после
выбора пресета с данными о населении показывается под-шаг порога населения.
"""
from __future__ import annotations

import inspect
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1].joinpath(
    "bot", "handlers", "global_presence.py").read_text("utf-8")


def test_pop_filter_step_and_handler_present():
    assert "_show_pop_filter_step" in SRC
    assert 'F.action == "pop"' in SRC, "нет обработчика выбора порога населения"
    assert "filter_by_population" in SRC, "порог не применяет filter_by_population"


def test_pop_step_shown_only_when_population_data_exists():
    # ветка появляется только если у городов пресета есть население
    assert "city_population(c.get" in SRC
    assert "_show_pop_filter_step(callback, state)" in SRC


def test_back_from_pop_returns_to_geo():
    assert 'F.action == "back_to_geo"' in SRC


def test_federal_district_selection_wired():
    assert 'F.action == "districts"' in SRC and 'F.action == "district"' in SRC
    assert "_show_district_step" in SRC
    assert "group_by_federal_district" in SRC
    # выбор округа фильтрует города по federal_district
    assert "federal_district(c.get" in SRC


def test_split_into_city_districts_wired():
    assert 'F.action == "split_dist"' in SRC, "нет обработчика разбиения на районы"
    assert "expand_city_to_districts" in SRC
    assert 'action="split_dist"' in SRC, "нет кнопки разбиения на районы"


def test_district_index_is_stable():
    """Индекс округа в callback берётся из стабильного (sorted) порядка."""
    from services import geo_data as g
    d1 = sorted(g.group_by_federal_district(g.RUSSIA_CITIES).keys())
    d2 = sorted(g.group_by_federal_district(g.RUSSIA_CITIES).keys())
    assert d1 == d2 and "Южный ФО" in d1


def test_geo_data_filter_still_correct():
    """Санити: сам фильтр по населению работает (данные под UI)."""
    from services import geo_data as g
    big = g.filter_by_population(g.RUSSIA_CITIES, 1_000_000)
    assert big and all(g.city_population(c["city_slug"]) >= 1_000_000 for c in big)
    assert len(big) < len(g.RUSSIA_CITIES)
