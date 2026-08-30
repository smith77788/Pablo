"""Ретеншен инвайта → организм: отток в мир-снимке + подсказка мозга.

«Пригласить» — половина дела; вторая — удержать. invite_retention уже считал
сводку для экрана, но мозг об оттоке не знал. Теперь world._retention кладёт
приток/отток/здоровье в снимок, а мозг предлагает усилить welcome при высоком
оттоке значимого объёма.
"""
from __future__ import annotations

import asyncio

def _run(coro):
    # Устойчиво к pytest-asyncio (asyncio_mode=auto): свой loop, а не глобальный
    # (его pytest-asyncio может закрыть/обнулить между тестами → RuntimeError
    # "no current event loop" при прямом asyncio.run в sync-тесте).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
import os

from services.organism.brain import build_suggestions
from services.organism import world

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "pressure": 20,
                  "governor_mult": 1.0, "governor_level": "green", "geo": {}},
        "ops": {"running": 1, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": {"label": "цель"},
        "growth": {"channels": 0, "growth_ops_7d": 0},
        "seo": {"scored": 0, "weak": 0, "worst": None},
        "retention": {"joined": 0, "left": 0, "retention_pct": None,
                      "churn_pct": None, "health": "unknown"},
        "bots": {"total": 0, "active": 0, "inactive": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_high_churn_raises_suggestion():
    s = build_suggestions(_snap(retention={
        "joined": 40, "left": 30, "retention_pct": 25.0, "churn_pct": 75.0, "health": "red"}))
    r = next((x for x in s if x["id"] == "retention_low"), None)
    assert r is not None
    assert r["action"]["kind"] == "retention"
    assert r["severity"] == "warn"
    assert "75" in r["title"]


def test_low_volume_does_not_shout():
    # отток красный, но приток мал (шум) → без подсказки
    s = build_suggestions(_snap(retention={
        "joined": 4, "left": 3, "retention_pct": 25.0, "churn_pct": 75.0, "health": "red"}))
    assert not any(x["id"] == "retention_low" for x in s)


def test_healthy_retention_no_suggestion():
    s = build_suggestions(_snap(retention={
        "joined": 100, "left": 5, "retention_pct": 95.0, "churn_pct": 5.0, "health": "green"}))
    assert not any(x["id"] == "retention_low" for x in s)
    # снимок без блока retention — тоже без подсказки
    base = _snap()
    del base["retention"]
    assert not any(x["id"] == "retention_low" for x in build_suggestions(base))


class _FakeValPool:
    def __init__(self, joined, left):
        self._seq = [joined, left]
        self._i = 0

    async def fetchval(self, q, *a):
        v = self._seq[self._i] if self._i < len(self._seq) else 0
        self._i += 1
        return v


def test_world_retention_summarizes():
    out = _run(
        world._retention(_FakeValPool(40, 30), 1))
    assert out["joined"] == 40 and out["left"] == 30
    assert out["retained"] == 10
    assert out["churn_pct"] == 75.0
    assert out["health"] == "red"


def test_world_retention_fail_open():
    class Boom:
        async def fetchval(self, q, *a):
            raise RuntimeError("db down")

    out = _run(world._retention(Boom(), 1))
    assert out["health"] == "unknown" and out["joined"] == 0


def test_world_snapshot_wires_retention():
    src = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "async def _retention" in src
    assert '"retention": await _retention(' in src
    assert "invite_retention" in src


def test_pulse_action_routes_retention():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "if (k==='retention') return openMassInvite();" in html
    assert "retention:'Настроить welcome'" in html
