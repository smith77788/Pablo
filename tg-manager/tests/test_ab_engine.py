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


def test_ab_results_endpoint_wired():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def ab_results" in src
    assert '"/api/miniapp/ab/results"' in src
    assert "ab_engine.pick_winner" in src
    assert "ab_batch" in src   # операции A/B помечаются общим батчем


def test_ab_results_frontend_present():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-abresults"' in html
    assert "function openAbResults" in html
    assert "/api/miniapp/ab/results" in html


# ── Follow-up по победителю ────────────────────────────────────────────────

def test_followup_targets_losers_not_winner():
    variants = [
        {"label": "A/B#1", "text": "win", "recipients": ["@w1", "@w2"]},
        {"label": "A/B#2", "text": "lose1", "recipients": ["@l1", "@l2"]},
        {"label": "A/B#3", "text": "lose2", "recipients": ["@l3"]},
    ]
    r = ab_engine.plan_winner_followup(variants, "A/B#1")
    assert r["winner_text"] == "win"
    assert r["targets"] == ["@l1", "@l2", "@l3"]     # только проигравшие
    assert "@w1" not in r["targets"] and "@w2" not in r["targets"]
    assert r["loser_variants"] == 2
    assert r["reason"] is None


def test_followup_dedups_and_excludes_winner_recipients():
    # получатель, попавший и в проигравший, и в победивший — не дублируем и не шлём
    variants = [
        {"label": "A", "text": "win", "recipients": ["@x", "@w"]},
        {"label": "B", "text": "lose", "recipients": ["@x", "@l", "@l"]},
    ]
    r = ab_engine.plan_winner_followup(variants, "A")
    assert r["targets"] == ["@l"]                    # @x уже у победителя, дубль @l схлопнут


def test_followup_no_winner_text():
    variants = [{"label": "A", "text": "  ", "recipients": ["@l"]}]
    r = ab_engine.plan_winner_followup(variants, "A")
    assert r["targets"] == [] and r["winner_text"] is None
    assert "текст" in (r["reason"] or "")


def test_followup_no_losers():
    variants = [{"label": "A", "text": "win", "recipients": ["@w"]}]
    r = ab_engine.plan_winner_followup(variants, "A")
    assert r["targets"] == []
    assert r["reason"]


def test_followup_blank_winner_label():
    r = ab_engine.plan_winner_followup([{"label": "A", "text": "t", "recipients": ["@a"]}], "")
    assert r["targets"] == [] and r["winner_text"] is None


def test_ab_followup_endpoint_wired():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def ab_followup" in src
    assert '"/api/miniapp/ab/followup"' in src
    assert "plan_winner_followup" in src
    # follow-up идёт через ту же массовую рассылку под губернатором
    assert "bulk_dm_adhoc" in src[src.index("async def ab_followup"):]


def test_ab_followup_frontend_present():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function runAbFollowup" in html
    assert "/api/miniapp/ab/followup" in html
