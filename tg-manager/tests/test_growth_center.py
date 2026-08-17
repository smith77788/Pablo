"""Мотор роста: чистая свёртка операций + подсказка мозга о застое роста."""
from __future__ import annotations

import os

from services import growth_center
from services.organism.brain import build_suggestions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_classify_op_maps_families():
    assert growth_center.classify_op("boost_views") == "boost"
    assert growth_center.classify_op("boost_subscribers") == "boost"
    assert growth_center.classify_op("niche_growth_post") == "growth_agent"
    assert growth_center.classify_op("self_promo_blast") == "self_promo"
    # presence и прочее — не рост
    assert growth_center.classify_op("global_presence_channel") is None
    assert growth_center.classify_op("bulk_dm_adhoc") is None


def test_summarize_growth_aggregates_by_tool():
    rows = [
        {"op_type": "boost_views", "status": "done", "done_items": 500, "total_items": 500, "created_at": 2},
        {"op_type": "boost_reactions", "status": "running", "done_items": 10, "total_items": 100, "created_at": 5},
        {"op_type": "self_promo_blast", "status": "done", "done_items": 40, "total_items": 40, "created_at": 3},
        {"op_type": "bulk_dm_adhoc", "status": "done", "done_items": 999, "total_items": 999, "created_at": 9},
    ]
    s = growth_center.summarize_growth(rows)
    assert s["tools"]["boost"]["ops"] == 2
    assert s["tools"]["boost"]["done"] == 1
    assert s["tools"]["boost"]["active"] == 1
    assert s["tools"]["boost"]["delivered"] == 510
    assert s["tools"]["boost"]["last_at"] == 5
    assert s["tools"]["self_promo"]["ops"] == 1
    assert s["tools"]["growth_agent"]["ops"] == 0
    # bulk_dm не учитывается в тоталах роста
    assert s["totals"]["ops"] == 3
    assert s["totals"]["delivered"] == 550
    assert s["totals"]["active_tools"] == 2


def test_summarize_empty():
    s = growth_center.summarize_growth([])
    assert s["totals"]["ops"] == 0
    assert all(t["ops"] == 0 for t in s["tools"].values())


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "pressure": 20,
                  "governor_mult": 1.0, "governor_level": "green", "geo": {}},
        "ops": {"running": 0, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": None,
        "growth": {"channels": 0, "growth_ops_7d": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_brain_growth_stall_suggestion():
    sugs = build_suggestions(_snap(growth={"channels": 3, "growth_ops_7d": 0}))
    g = next((s for s in sugs if s["id"] == "growth_stall"), None)
    assert g is not None
    assert g["action"]["kind"] == "growth"


def test_brain_no_stall_when_growth_active_or_no_channels():
    assert not any(s["id"] == "growth_stall"
                   for s in build_suggestions(_snap(growth={"channels": 3, "growth_ops_7d": 5})))
    assert not any(s["id"] == "growth_stall"
                   for s in build_suggestions(_snap(growth={"channels": 0, "growth_ops_7d": 0})))


def test_recommend_plan_splits_goal_across_tools():
    p = growth_center.recommend_plan(1000, 14, n_accounts=20, daily_capacity=400)
    kinds = [s["kind"] for s in p["steps"]]
    assert kinds == ["invite", "self_promo", "growth_agent", "boost"]
    # веса суммируются в 1.0, цели — доли от goal
    assert abs(sum(s["weight"] for s in p["steps"]) - 1.0) < 1e-9
    assert p["steps"][0]["target"] == 500   # invite = 50%
    assert p["steps"][3]["target"] == 100   # boost = 10%
    assert p["seo"]["kind"] == "seo"
    assert p["goal"] == 1000 and p["deadline_days"] == 14


def test_recommend_plan_flags_infeasible_invite_with_no_fleet():
    p = growth_center.recommend_plan(100000, 1, n_accounts=0, daily_capacity=0)
    assert p["feasible_invite"] is False
    assert p["verdict"]


def test_recommend_plan_clamps_bad_input():
    p = growth_center.recommend_plan(0, 0, n_accounts=-5)
    assert p["goal"] == 1 and p["deadline_days"] == 1
    assert p["fleet_accounts"] == 0


def test_plan_endpoint_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def growth_plan" in api
    assert '"/api/miniapp/growth/plan"' in api
    assert "growth_center.recommend_plan" in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function runGrowthPlan" in html
    assert "/api/miniapp/growth/plan" in html


def test_endpoint_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def growth_overview" in api
    assert '"/api/miniapp/growth/overview"' in api
    assert "growth_center.summarize_growth" in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-growthhub"' in html
    assert "function openGrowthHub" in html
    assert "/api/miniapp/growth/overview" in html
