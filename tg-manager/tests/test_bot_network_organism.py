"""Сеть ботов → организм: здоровье сети в мир-снимке + подсказка мозга."""
from __future__ import annotations

import os

from services.organism.brain import build_suggestions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "pressure": 20,
                  "governor_mult": 1.0, "governor_level": "green", "geo": {}},
        "ops": {"running": 0, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": None,
        "growth": {"channels": 0, "growth_ops_7d": 0},
        "bots": {"total": 0, "active": 0, "inactive": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_inactive_bots_raise_suggestion():
    sugs = build_suggestions(_snap(bots={"total": 5, "active": 3, "inactive": 2}))
    b = next((s for s in sugs if s["id"] == "bots_inactive"), None)
    assert b is not None
    assert b["action"]["kind"] == "bots"


def test_all_active_or_no_bots_no_suggestion():
    assert not any(s["id"] == "bots_inactive"
                   for s in build_suggestions(_snap(bots={"total": 4, "active": 4, "inactive": 0})))
    assert not any(s["id"] == "bots_inactive"
                   for s in build_suggestions(_snap(bots={"total": 0, "active": 0, "inactive": 0})))


def test_world_snapshot_has_bots_block():
    src = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "async def _bots" in src
    assert '"bots": await _bots(' in src
    assert "managed_bots WHERE added_by" in src


def test_pulse_action_routes_bots():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "if (k==='bots') return openNetwork();" in html
