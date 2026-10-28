"""Ступень воронки открывается списком тех самых людей.

Принцип проекта: число на экране обязано вести в срез, а не оставаться числом.
«Эффективная» ступень считается так же, как воронка (effective_value):
просроченная строка опускается на рунг, поэтому ступень V собирают и сырое V
(не просрочено), и строки на рунг выше, которые просрочены и распались до V.
"""
from __future__ import annotations

import asyncio
import os

from services import virtual_layer as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Pool:
    def __init__(self, state_rows, names=None):
        self._state = state_rows
        self._names = names or {}   # eid -> name

    async def fetch(self, q, *a):
        if "GROUP BY value, expired" in q:          # агрегат для total
            agg = {}
            for r in self._state:
                k = (r["value"], r["expired"])
                agg[k] = agg.get(k, 0) + 1
            return [{"value": v, "expired": e, "c": c} for (v, e), c in agg.items()]
        if "FROM virtual_states" in q:              # сам список
            return self._state
        if "unified_contacts" in q:
            return [{"eid": k, "name": v} for k, v in self._names.items()]
        return []        # managed_bots и пр.


def _run(c):
    return asyncio.run(c)


def _row(eid, value, expired, conf=0.5):
    return {"entity_id": eid, "value": value, "confidence": conf,
            "expires_at": None, "expired": expired}


def test_segment_collects_raw_and_decayed_rows():
    # ступень «интерес»: сырой «интерес» (жив) + «квалифицирован» просроченный
    # (распадётся до «интереса»). А живой «квалифицирован» и просроченный
    # «интерес» (станет «присматривается») сюда НЕ попадают.
    rows = [
        _row("a", "interested", False),      # → включён
        _row("b", "qualified", True),        # просрочен → интерес → включён
        _row("c", "qualified", False),       # жив → квалифицирован → НЕ он
        _row("d", "interested", True),       # просрочен → присматривается → НЕ он
    ]
    pool = _Pool(rows, {"a": "Аня", "b": "Борис"})
    d = _run(V.segment(pool, 7, "interested"))
    ids = {p["entity_id"] for p in d["people"]}
    assert ids == {"a", "b"}
    assert d["label"] == "Интерес"
    # имена подтянуты, срок жизни рунга проброшен
    names = {p["entity_id"]: p.get("name") for p in d["people"]}
    assert names["a"] == "Аня" and names["b"] == "Борис"
    assert all(p["ttl_hours"] == 96 for p in d["people"])   # TTL «interested» = 96ч
    assert d["total"] == 2                                    # настоящий размер ступени


def test_segment_total_is_honest_when_truncated():
    # потолок не должен выдаваться за весь список: total считает всех, people — срез
    rows = [_row(f"u{i}", "interested", False) for i in range(5)]
    d = _run(V.segment(_Pool(rows), 7, "interested", limit=2))
    assert len(d["people"]) == 2 and d["total"] == 5


def test_segment_terminal_value_has_no_rung_above():
    # «потерян» — терминал: кандидат только он сам, без «рунга выше».
    rows = [_row("x", "lost", False)]
    d = _run(V.segment(_Pool(rows, {"x": "X"}), 7, "lost"))
    assert [p["entity_id"] for p in d["people"]] == ["x"]


def test_segment_fail_open_on_broken_pool():
    class _Boom:
        async def fetch(self, q, *a):
            raise RuntimeError("база лежит")
    d = _run(V.segment(_Boom(), 7, "ready"))
    assert d["people"] == [] and d["value"] == "ready"


# ── проводка ──────────────────────────────────────────────────────────────────

def test_segment_route_validates_and_wired():
    from tests.miniapp_source import api_source
    api = api_source()
    assert 'add_get("/api/miniapp/vlayer/segment/{value}"' in api
    assert "await _vl.segment(pool, uid, value)" in api
    assert "Неизвестная ступень" in api        # мусорная ступень → 400, не 500


def test_screen_rungs_open_the_segment():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "async function openVLayerSegment" in html
    assert "openVLayerSegment('" in html            # ступень кликабельна
    assert "/api/miniapp/vlayer/segment/" in html
    # из списка человек открывается в свою карточку
    assert "кто это →" in html
    # честный потолок: «показано X из Y», а не потолок как весь список
    assert "показано ${num(people.length)} из ${num(total)}" in html
