"""Срез «⏳ Вот-вот остынут»: кого дожать ДО распада.

Распад снимает состояние на ближайшем проходе; дожать надо до него, иначе это уже
реактивация потерянного. Сводка отдаёт живые состояния interested+ со сроком в
ближайшие сутки, отсортированные по близости распада; экран показывает их вверху.
"""
from __future__ import annotations

import asyncio
import os

from services import virtual_layer as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Pool:
    """Отдаёт cooling-строки на запрос с окном 24 часа; остальное — пусто."""
    def __init__(self, cooling_rows, names=None):
        self._cooling = cooling_rows
        self._names = names or {}

    async def fetch(self, q, *a):
        if "interval '24 hours'" in q:
            return self._cooling
        if "unified_contacts" in q:
            return [{"eid": k, "name": v} for k, v in self._names.items()]
        return []

    async def fetchrow(self, q, *a):
        return None


def _run(c):
    return asyncio.run(c)


def test_overview_includes_cooling_with_names():
    rows = [
        {"entity_type": "user", "entity_id": "a", "value": "ready",
         "confidence": 0.9, "expires_at": None},
        {"entity_type": "user", "entity_id": "b", "value": "interested",
         "confidence": 0.6, "expires_at": None},
    ]
    out = _run(V.overview(_Pool(rows, {"a": "Аня", "b": "Борис"}), 7))
    cool = out["cooling"]
    assert {c["entity_id"] for c in cool} == {"a", "b"}
    assert {c["entity_id"]: c.get("name") for c in cool} == {"a": "Аня", "b": "Борис"}
    assert cool[0]["label"] == "Готов купить"      # русская подпись


def test_cooling_empty_by_default_and_failopen():
    # пустой пул — пустой список, не падение
    out = _run(V.overview(_Pool([]), 7))
    assert out["cooling"] == []


def test_screen_shows_cooling_section():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "Вот-вот остынут" in html
    assert "d.cooling" in html
    # каждый открывается в карточку
    assert "cooling.slice(0, 30)" in html


def test_api_passes_cooling_with_iso_dates():
    from tests.miniapp_source import api_source
    api = api_source()
    assert '"cooling": cooling' in api
    assert 'c["expires_at"].isoformat()' in api
