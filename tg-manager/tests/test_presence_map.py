"""Карта присутствия: чистая свёртка планов + покрытие; wiring эндпоинта/UI."""
from __future__ import annotations

import os

from services import presence_map

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_summarize_targets_counts_by_country():
    rows = [
        {"country": "Germany", "country_code": "de", "status": "done"},
        {"country": "Germany", "country_code": "de", "status": "pending"},
        {"country": "France", "country_code": "fr", "status": "failed"},
        {"country": "France", "country_code": "fr", "status": "running"},
    ]
    s = presence_map.summarize_targets(rows)
    de = s["countries"]["DE"]
    assert de["planned"] == 2 and de["done"] == 1 and de["pending"] == 1
    fr = s["countries"]["FR"]
    assert fr["planned"] == 2 and fr["done"] == 0 and fr["pending"] == 1  # failed не pending
    # пробелы — страны с pending>0
    assert set(s["gaps"]) == {"DE", "FR"}


def test_summarize_targets_no_gaps_when_all_done():
    rows = [{"country": "Italy", "country_code": "it", "status": "done"}]
    s = presence_map.summarize_targets(rows)
    assert s["gaps"] == []
    assert s["countries"]["IT"]["done"] == 1


def test_summarize_targets_empty():
    s = presence_map.summarize_targets([])
    assert s["countries"] == {} and s["gaps"] == []


def test_coverage_score_levels():
    assert presence_map.coverage_score(1, 0)["level"] == "narrow"
    assert presence_map.coverage_score(3, 1)["level"] == "moderate"
    assert presence_map.coverage_score(6, 0)["level"] == "wide"
    c = presence_map.coverage_score(4, 2)
    assert c["distinct_geo"] == 4 and c["gaps"] == 2


def test_endpoint_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def presence_map_overview" in api
    assert '"/api/miniapp/presence/map"' in api
    assert "presence_map.summarize_targets" in api
    assert "geo_router.summarize_distribution" in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-presencemap"' in html
    assert "function openPresenceMap" in html
    assert "/api/miniapp/presence/map" in html
