"""Инвайтинг: сегмент контактов как источник аудитории (единый движок)."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api():
    return open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def test_submit_accepts_segment_source():
    src = _api()
    assert '"parsed", "crm", "bot_users", "import_list", "segment"' in src
    i = src.index('if source == "segment":')
    win = src[i:i + 500]
    assert "saved_segment_id" in win and "segment_filters" in win


def test_audience_size_supports_segment():
    src = _api()
    assert '"parsed", "crm", "bot_users", "segment"' in src
    i = src.index('elif source == "segment":')
    win = src[i:i + 500]
    assert "count_segment" in win and "get_segment_filters" in win


def test_executor_resolves_segment_via_engine():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = ow.index('elif source == "segment":')
    fn = ow[i:i + 900]
    # единый движок сегментов, а не legacy crm_contacts
    assert "resolve_segment" in fn
    assert "get_segment_filters" in fn
    assert "unified" in fn.lower() or "repository" in fn


def test_frontend_segment_source_ui():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'value="segment"' in html
    assert "massInviteSegmentField" in html
    assert "function loadInviteSegments" in html
    assert "body.saved_segment_id" in html
    assert "/api/miniapp/uch/segments" in html
