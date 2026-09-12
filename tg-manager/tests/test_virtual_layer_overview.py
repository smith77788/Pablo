"""Virtual Layer: read-поверхность (обзор) — видимость слоя владельцу.

Движок без экрана невидим: владелец не может проверить, что модель поведения
реальна. Здесь проверяется цепочка обзора: агрегатор считает воронку и «горячих»
из virtual_states, лента событий берётся из spine, эндпоинт их сводит, экран
читает ровно те ключи, что отдаёт бэкенд.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from services import virtual_layer as V

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
UI = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


# ── Агрегатор overview (чистая сборка поверх заглушки пула) ────────────────

class _Pool:
    def __init__(self, funnel_rows, hot_rows):
        self._funnel = funnel_rows
        self._hot = hot_rows

    async def fetch(self, q, *a):
        if "GROUP BY value" in q:
            return self._funnel
        if "value = ANY" in q:
            return self._hot
        return []


def test_overview_builds_funnel_in_ladder_order():
    pool = _Pool(
        funnel_rows=[{"value": "ready", "c": 3}, {"value": "curious", "c": 10},
                     {"value": "interested", "c": 5}],
        hot_rows=[])
    ov = asyncio.run(V.overview(pool, 7))
    order = [f["value"] for f in ov["funnel"]]
    assert order == ["curious", "interested", "ready"]   # по лестнице, не по вводу
    assert ov["total"] == 18


def test_overview_labels_are_russian():
    pool = _Pool([{"value": "ready", "c": 1}], [])
    ov = asyncio.run(V.overview(pool, 7))
    assert ov["funnel"][0]["label"] == "Готов купить"


def test_overview_hot_lists_top_rungs():
    pool = _Pool(
        [{"value": "ready", "c": 1}],
        [{"entity_type": "user", "entity_id": "c1", "value": "ready",
          "confidence": 0.9, "updated_at": None}])
    ov = asyncio.run(V.overview(pool, 7))
    assert ov["hot"][0]["entity_id"] == "c1" and ov["hot"][0]["confidence"] == 0.9


def test_overview_empty_is_safe():
    ov = asyncio.run(V.overview(_Pool([], []), 7))
    assert ov == {"funnel": [], "hot": [], "total": 0}


def test_overview_survives_a_broken_pool():
    class _Boom:
        async def fetch(self, q, *a):
            raise RuntimeError("db")
    ov = asyncio.run(V.overview(_Boom(), 7))
    assert ov["total"] == 0


# ── Виды виртуальных событий и подписи ─────────────────────────────────────

def test_virtual_event_kinds_cover_the_ladder_events():
    assert "purchase_intent_detected" in V.VIRTUAL_EVENT_KINDS
    assert "user_lost_interest" in V.VIRTUAL_EVENT_KINDS


def test_every_event_kind_has_a_russian_label():
    for k in V.VIRTUAL_EVENT_KINDS:
        assert V.EVENT_LABEL.get(k), f"{k} без русской подписи"


# ── Проводка: эндпоинт, роут, экран ────────────────────────────────────────

def test_endpoint_and_route_exist():
    assert "async def vlayer_overview" in API
    assert 'add_get("/api/miniapp/vlayer/overview"' in API


def test_endpoint_pulls_events_from_spine_by_kind():
    body = API[API.find("async def vlayer_overview"):
               API.find("async def vlayer_overview") + 1600]
    assert "recent_events" in body and "VIRTUAL_EVENT_KINDS" in body


def test_screen_exists_and_is_reachable():
    assert 'id="s-vlayer"' in UI
    assert 'onclick="openVLayer()"' in UI
    assert re.search(r"async function openVLayer\s*\(", UI)


def test_screen_reads_only_keys_the_endpoint_returns():
    """Ключи, на которых держится экран, должны отдаваться бэкендом."""
    for key in ('"funnel"', '"hot"', '"events"', '"total"'):
        assert key in API, key
    for read in ("d.funnel", "d.events", "d.hot", "d.total"):
        assert read in UI, read
