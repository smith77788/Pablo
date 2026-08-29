"""Пульс-Дайджест: цельный отчёт организма из существующих модулей.

Композиция world.snapshot + brain(suggestions/narrative) + spine-тренды в один
отчёт. compose_digest — чистая функция, покрыта юнит-тестами.
"""
from __future__ import annotations

import os

from services.organism.digest import compose_digest, _flat_metrics, _headline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snap(**over):
    base = {
        "fleet": {"accounts": 10, "active": 8, "dead": 2, "bans_24h": 0,
                  "pressure": 10, "governor_mult": 1.0, "governor_level": "green"},
        "ops": {"running": 1, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 100, "hot_leads": 5, "intents_24h": 3},
        "growth": {"channels": 4, "growth_ops_7d": 2},
        "bots": {"total": 3, "active": 3, "community_nodes": 1, "community_empty": 0},
        "vault": {"health": "ok", "stale_days": 0},
    }
    base.update(over)
    return base


def test_compose_has_all_sections_and_headline():
    d = compose_digest(_snap(), suggestions=[], narrative_text="обзор")
    keys = {s["key"] for s in d["sections"]}
    assert keys == {"fleet", "audience", "growth", "network", "risks"}
    assert d["narrative"] == "обзор"
    assert d["headline"].startswith("🟢")   # всё здорово
    assert d["has_prev"] is False


def test_trend_up_good_and_bad():
    prev = _flat_metrics(_snap())            # base: active=8, dead=2, contacts=100
    snap2 = _snap(fleet={"accounts": 12, "active": 11, "dead": 4, "bans_24h": 0,
                         "pressure": 10, "governor_mult": 1.0, "governor_level": "green"},
                  graph={"contacts": 130, "hot_leads": 5, "intents_24h": 0})
    d = compose_digest(snap2, suggestions=[], prev_metrics=prev)
    fleet_sec = next(s for s in d["sections"] if s["key"] == "fleet")
    active = next(x for x in fleet_sec["stats"] if x["label"] == "Активных аккаунтов")
    dead = next(x for x in fleet_sec["stats"] if x["label"] == "Мёртвых")
    assert active["trend"] == {"delta": 3, "dir": "up", "good": True}    # рост активных — хорошо
    assert dead["trend"] == {"delta": 2, "dir": "up", "good": False}     # рост мёртвых — плохо


def test_recommendations_sorted_by_severity_and_capped():
    sugs = [
        {"id": "a", "severity": "info", "title": "i", "why": "", "action": {}},
        {"id": "b", "severity": "urgent", "title": "u", "why": "", "action": {}},
        {"id": "c", "severity": "warn", "title": "w", "why": "", "action": {}},
        {"id": "d", "severity": "opportunity", "title": "o", "why": "", "action": {}},
        {"id": "e", "severity": "info", "title": "i2", "why": "", "action": {}},
        {"id": "f", "severity": "info", "title": "i3", "why": "", "action": {}},
    ]
    d = compose_digest(_snap(), suggestions=sugs)
    titles = [r["title"] for r in d["recommendations"]]
    assert titles[0] == "u" and titles[1] == "w" and titles[2] == "o"  # urgent→warn→opp
    assert len(d["recommendations"]) == 5                               # кап 5


def test_headline_variants():
    assert _headline(_snap(fleet={"accounts": 0})).startswith("🚀")
    assert _headline(_snap(fleet={"accounts": 5, "bans_24h": 2, "governor_level": "red"})).startswith("🔴")
    stall = _snap(growth={"channels": 3, "growth_ops_7d": 0})
    assert _headline(stall).startswith("🟡")


def test_endpoint_route_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def organism_digest" in api
    assert '"/api/miniapp/organism/digest"' in api
    assert "from services.organism import digest" in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-digest"' in html
    assert "async function openDigest" in html
    assert "/api/miniapp/organism/digest" in html


def test_new_trend_metrics_retained_and_seo():
    from services.organism.digest import _flat_metrics, _TREND_METRICS, _LOWER_IS_BETTER
    assert "retained" in _TREND_METRICS and "seo_weak" in _TREND_METRICS
    assert "seo_weak" in _LOWER_IS_BETTER  # рост слабых по SEO — негатив
    m = _flat_metrics({"retention": {"retained": 12}, "seo": {"weak": 4}})
    assert m["retained"] == 12 and m["seo_weak"] == 4
    # None/отсутствие — безопасно 0
    assert _flat_metrics({})["retained"] == 0
