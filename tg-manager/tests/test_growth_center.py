"""Мотор роста: чистая свёртка операций + подсказка мозга о застое роста."""
from __future__ import annotations

import os

from services import growth_center
from services.organism.brain import build_suggestions
from tests.miniapp_source import api_source

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


def test_the_fixed_share_plan_is_gone():
    """Раскладка цели по вшитым долям 50/20/20/10 удалена (09.11.2026).

    Она не смотрела ни на то, что растят, ни на то, чем владелец располагает,
    цель не сохранялась и прогресс не считался — владелец назвал модуль
    бесполезным. Цели и рычаги теперь в services/growth_plan.py
    (tests/test_growth_plan_postgres.py). Мёртвая функция ждёт, пока её
    выберут вместо живой, поэтому проверяем, что она не вернулась.
    """
    assert not hasattr(growth_center, "recommend_plan")
    api = api_source()
    assert '"/api/miniapp/growth/plan"' not in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function runGrowthPlan" not in html
    assert "/api/miniapp/growth/goals" in open(
        os.path.join(ROOT, "mini_app", "screens", "growth_plan.js"), encoding="utf-8").read()


def test_endpoint_and_ui_wired():
    api = api_source()
    assert "async def growth_overview" in api
    assert '"/api/miniapp/growth/overview"' in api
    assert "growth_center.summarize_growth" in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-growthhub"' in html
    assert "function openGrowthHub" in html
    assert "/api/miniapp/growth/overview" in html
