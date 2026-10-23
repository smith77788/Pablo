"""Гео-иерархия РФ: Федеральный округ → регион → город.

Разблокирует дерево видения «Россия → Южный ФО → Краснодарский край → Сочи».
Карта регион→округ отдельная (не трогает записи городов).
"""
from __future__ import annotations

from services import geo_data as g


def test_federal_district_lookup():
    assert g.federal_district("Krasnodar Krai") == "Южный ФО"
    assert g.federal_district("Moscow") == "Центральный ФО"
    assert g.federal_district("Sverdlovsk Oblast") == "Уральский ФО"
    assert g.federal_district("Nonexistent Oblast") is None


def test_all_present_ru_regions_have_a_district():
    """Каждый регион, встречающийся в RUSSIA_CITIES, отнесён к округу (нет сирот)."""
    regions = {c.get("region") for c in g.RUSSIA_CITIES if c.get("region")}
    unmapped = [r for r in regions if g.federal_district(r) is None]
    assert not unmapped, f"регионы без федерального округа: {unmapped}"


def test_group_by_federal_district_shape():
    tree = g.group_by_federal_district(g.RUSSIA_CITIES)
    assert "Южный ФО" in tree
    assert "Krasnodar Krai" in tree["Южный ФО"]
    sochi = [c for c in tree["Южный ФО"]["Krasnodar Krai"] if c["city"] == "Sochi"]
    assert sochi, "Сочи должен попасть в Краснодарский край / Южный ФО"
    # ни один город РФ не потерян
    total = sum(len(cities) for regs in tree.values() for cities in regs.values())
    assert total == len(g.RUSSIA_CITIES)


def test_group_by_region_preserves_all():
    grouped = g.group_by_region(g.RUSSIA_CITIES)
    assert sum(len(v) for v in grouped.values()) == len(g.RUSSIA_CITIES)


def test_non_ru_city_grouped_under_region_not_district():
    cities = [{"city": "Berlin", "region": "Berlin", "country_code": "de"}]
    tree = g.group_by_federal_district(cities)
    assert "Berlin" in tree and "Berlin" in tree["Berlin"]
