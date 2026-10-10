"""Карточка-360: чистое слияние касаний контакта + wiring эндпоинта/UI."""
from __future__ import annotations

import datetime as dt
import os

from services.contacts_hub import timeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(day):
    return dt.datetime(2026, 1, day, 12, 0, tzinfo=dt.timezone.utc)


def test_merges_and_sorts_desc():
    tl = timeline.build_timeline(
        history=[{"field": "first_name", "old_value": "A", "new_value": "B", "created_at": _t(1)}],
        events=[{"kind": "intent", "payload": {"text": "цена?", "stage": "proposal",
                                               "tags": ["горячий"]}, "created_at": _t(3)}],
        sources=[{"account_id": 7, "discovered_at": _t(2)}],
        crm={"stage": "won", "updated_at": _t(4)})
    kinds = [i["kind"] for i in tl]
    # новые сверху: crm(4) > intent(3) > source(2) > history(1)
    assert kinds == ["crm", "intent", "source", "history"]
    intent = tl[1]
    assert intent["icon"] == "💡"
    assert "цена" in intent["detail"] and "proposal" in intent["detail"]


def test_drops_items_without_timestamp():
    tl = timeline.build_timeline(
        history=[{"field": "notes", "old_value": "", "new_value": "x", "created_at": None}],
        crm={"stage": "lead", "updated_at": None})
    assert tl == []


def test_history_title_humanized():
    tl = timeline.build_timeline(
        history=[{"field": "username", "old_value": "a", "new_value": "b", "created_at": _t(1)}])
    assert "Username" in tl[0]["title"]
    assert tl[0]["detail"] == "a → b"


def test_limit_caps_items():
    hist = [{"field": "notes", "old_value": "", "new_value": str(i), "created_at": _t(1 + i % 27)}
            for i in range(100)]
    tl = timeline.build_timeline(history=hist, limit=10)
    assert len(tl) == 10


def test_empty_inputs():
    assert timeline.build_timeline() == []


def test_endpoint_uses_builder_and_no_duplicate_route():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "timeline.build_timeline" in api
    assert "payload->>'contact_id'" in api
    # ровно один маршрут /timeline (без дубликата)
    assert api.count('/api/miniapp/uch/contacts/{contact_id}/timeline"') == 1


def test_frontend_renders_icon_title():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    i = html.index("async function openContactTimeline")
    window = html[i:i + 1200]
    assert "t.icon" in window and "t.title" in window and "t.ts" in window
