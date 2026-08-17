"""A/B рассылка: чистая раскладка аудитории и выбор победителя + wiring."""
from __future__ import annotations

import os

from services import ab_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_clean_variants_trims_and_caps():
    v = ab_engine.clean_variants(["  a ", "", "b", "c", "d", "e"])
    assert v == ["a", "b", "c", "d"]          # пустые убраны, максимум 4
    assert ab_engine.clean_variants(None) == []


def test_split_audience_balanced():
    groups = ab_engine.split_audience(list(range(10)), 3)
    assert [len(g) for g in groups] == [4, 3, 3]   # различие ≤ 1
    # без потерь и дублей
    flat = [x for g in groups for x in g]
    assert sorted(flat) == list(range(10))


def test_split_audience_single_variant():
    groups = ab_engine.split_audience([1, 2, 3], 1)
    assert groups == [[1, 2, 3]]


def test_pick_winner_needs_sample_and_significance():
    # Малая выборка — лидер есть, но не уверены.
    small = [{"variant": 0, "sent": 10, "converted": 5},
             {"variant": 1, "sent": 10, "converted": 2}]
    r = ab_engine.pick_winner(small)
    assert r["leader"] == 0
    assert r["winner"] is None and r["confident"] is False


def test_pick_winner_confident_on_clear_separation():
    big = [{"variant": 0, "sent": 500, "converted": 250},   # 50%
           {"variant": 1, "sent": 500, "converted": 100}]   # 20%
    r = ab_engine.pick_winner(big)
    assert r["confident"] is True
    assert r["winner"] == 0
    assert abs(r["z"]) >= 1.96


def test_pick_winner_empty():
    r = ab_engine.pick_winner([])
    assert r["winner"] is None and r["rates"] == {}


def test_segment_message_supports_ab_variants():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    i = src.index("async def uch_segment_message")
    window = src[i:i + 3200]
    assert "ab_engine.clean_variants" in window
    assert "ab_engine.split_audience" in window
    assert "ab_variant" in window
