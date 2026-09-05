"""Ретеншен инвайта: чистая свёртка + left-события chat_guard + wiring."""
from __future__ import annotations

import os

from services import invite_retention

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_summarize_basic():
    s = invite_retention.summarize(100, 20)
    assert s["retained"] == 80
    assert s["retention_pct"] == 80.0
    assert s["churn_pct"] == 20.0


def test_summarize_no_joins_is_unknown():
    s = invite_retention.summarize(0, 5)
    assert s["retention_pct"] is None and s["retained"] is None


def test_summarize_left_exceeds_joined_clamps():
    s = invite_retention.summarize(10, 25)
    assert s["retained"] == 0
    assert s["retention_pct"] == 0.0


def test_health_thresholds():
    assert invite_retention.health(None) == "unknown"
    assert invite_retention.health(90) == "green"
    assert invite_retention.health(60) == "amber"
    assert invite_retention.health(30) == "red"


def test_chat_guard_emits_left_event():
    src = open(os.path.join(ROOT, "bot", "handlers", "chat_guard.py"), encoding="utf-8").read()
    i = src.index("async def on_chat_member")
    window = src[i:i + 1200]
    assert 'spine.emit' in window and '"left"' in window
    assert 'new in {"left", "kicked"}' in window


def test_endpoint_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def invite_retention_overview" in api
    assert '"/api/miniapp/invite/retention"' in api
    assert "invite_retention.summarize" in api
    assert "kind='left'" in api
    from tests.miniapp_source import miniapp_source
    html = miniapp_source()
    assert "function loadInviteRetention" in html
    assert "/api/miniapp/invite/retention" in html
