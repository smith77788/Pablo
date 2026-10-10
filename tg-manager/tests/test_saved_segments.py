"""Сохранённые сегменты: умный фильтр как сущность, переиспользуемый в действиях."""
from __future__ import annotations

import asyncio
import json
import os

from services.contacts_hub import repository as repo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_clean_filters_keeps_only_valid_keys():
    f = repo._clean_filters({"gender": "m", "crm_stage": "won", "junk": 1,
                             "favorite_only": False, "tag": "vip", "search": ""})
    assert f == {"gender": "m", "crm_stage": "won", "tag": "vip"}   # пустое/мусор отброшены


class _FakePool:
    def __init__(self, filters_row=None):
        self.saved = {}
        self._filters_row = filters_row

    async def fetchval(self, sql, *a):
        if "INSERT INTO saved_segments" in sql:
            self.saved[1] = a
            return 1
        if "COUNT(*)" in sql:
            return 42
        return 0

    async def fetch(self, sql, *a):
        return [{"id": 1, "name": "Горячие",
                 "filters": json.dumps({"crm_stage": "negotiation"}), "created_at": None}]

    async def fetchrow(self, sql, *a):
        if "SELECT filters FROM saved_segments" in sql:
            return self._filters_row
        return None

    async def execute(self, sql, *a):
        return "DELETE 1"


def test_save_segment_cleans_and_names():
    p = _FakePool()
    sid = _run(repo.save_segment(p, 5, "", {"gender": "f", "bad": 1}))
    assert sid == 1
    # name дефолтится, фильтры очищены (в INSERT попали owner, name, json)
    owner, name, fjson = p.saved[1]
    assert owner == 5 and name == "Сегмент"
    assert json.loads(fjson) == {"gender": "f"}


def test_list_segments_adds_live_count():
    segs = _run(repo.list_segments(_FakePool(), 5))
    assert segs[0]["name"] == "Горячие" and segs[0]["count"] == 42
    assert segs[0]["filters"] == {"crm_stage": "negotiation"}


def test_get_segment_filters_parses_json():
    p = _FakePool(filters_row={"filters": json.dumps({"tag": "vip"})})
    assert _run(repo.get_segment_filters(p, 5, 1)) == {"tag": "vip"}
    p2 = _FakePool(filters_row=None)
    assert _run(repo.get_segment_filters(p2, 5, 99)) is None   # чужой/нет → None


def test_backend_endpoints_and_saved_id_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    for r in ('"/api/miniapp/uch/segments"', '/segments/save"', '/segments/{seg_id}"'):
        assert r in api
    # сегмент-действия принимают saved_segment_id (единый резолвер)
    assert "get_segment_filters" in api and 'body.get("saved_segment_id")' in api


def test_frontend_saved_segments_ui():
    ui = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function saveCurrentSegment" in ui and "function openSavedSegments" in ui
    assert "segmentActFromSaved" in ui and 'id="s-segments"' in ui
    # действие из сохранённого сегмента шлёт saved_segment_id
    assert "saved_segment_id: seg.id" in ui
