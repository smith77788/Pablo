"""Детектор аномалий → организм: активные аномалии в снимке + подсказка мозга.

anomaly_detector писал anomaly_events, но никто их не читал — чистый остров.
Теперь world._anomalies кладёт critical/warning за 24ч в снимок, а мозг поднимает
срочную (critical) или предупреждающую (warning) подсказку в раздел здоровья —
ранний сигнал бана.
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
                  "governor_mult": 1.0, "governor_level": "green", "geo": {}, "bans_24h": 0},
        "ops": {"running": 1, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": {"label": "цель"},
        "anomalies": {"critical": 0, "warning": 0, "top": None},
        "bots": {"total": 0, "active": 0, "inactive": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_critical_anomaly_is_urgent():
    s = build_suggestions(_snap(anomalies={"critical": 2, "warning": 1, "top": "Всплеск ошибок"}))
    a = next((x for x in s if x["id"] == "anomaly_crit"), None)
    assert a is not None
    assert a["severity"] == "urgent" and a["action"]["kind"] == "health"
    assert "Всплеск ошибок" in a["why"]


def test_warning_only_is_warn():
    s = build_suggestions(_snap(anomalies={"critical": 0, "warning": 3, "top": "Падение trust"}))
    assert not any(x["id"] == "anomaly_crit" for x in s)
    a = next((x for x in s if x["id"] == "anomaly_warn"), None)
    assert a is not None and a["severity"] == "warn"


def test_no_anomalies_no_suggestion():
    s = build_suggestions(_snap())
    assert not any(x["id"] in ("anomaly_crit", "anomaly_warn") for x in s)
    base = _snap()
    del base["anomalies"]
    assert not any(x["id"] in ("anomaly_crit", "anomaly_warn") for x in build_suggestions(base))


class _FakePool:
    def __init__(self, crit, warn, top):
        self._row = {"crit": crit, "warn": warn}
        self._top = top

    async def fetchrow(self, q, *a):
        return self._row

    async def fetchval(self, q, *a):
        return self._top


def test_world_anomalies_counts():
    out = _run(
        world._anomalies(_FakePool(2, 3, "Латентность"), 1))
    assert out["critical"] == 2 and out["warning"] == 3 and out["top"] == "Латентность"


def test_world_anomalies_fail_open():
    class Boom:
        async def fetchrow(self, q, *a):
            raise RuntimeError("db down")

    out = _run(world._anomalies(Boom(), 1))
    assert out == {"critical": 0, "warning": 0, "top": None}


def test_world_snapshot_wires_anomalies():
    src = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "async def _anomalies" in src
    assert '"anomalies": await _anomalies(' in src
    assert "anomaly_events" in src


def test_pulse_action_routes_health():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "if (k==='health') return openHealth();" in html
    assert "health:'Открыть здоровье'" in html


def test_narrative_mentions_critical_anomaly():
    from services.organism.brain import narrative
    n = narrative(_snap(anomalies={"critical": 2, "warning": 0, "top": "x"}))
    assert "критич" in n and "2" in n
    # без аномалий — не упоминает
    assert "критич" not in narrative(_snap())


def test_restricted_accounts_suggestion():
    from services.organism.brain import build_suggestions
    s = build_suggestions(_snap(fleet={"accounts": 20, "active": 15, "dead": 0,
                                       "restricted": 3, "pressure": 20,
                                       "governor_mult": 1.0, "governor_level": "green",
                                       "geo": {}, "bans_24h": 0}))
    r = next((x for x in s if x["id"] == "accounts_restricted"), None)
    assert r is not None and r["action"]["kind"] == "health" and "3" in r["title"]
    # нет ограниченных — нет подсказки
    assert not any(x["id"] == "accounts_restricted"
                   for x in build_suggestions(_snap()))
