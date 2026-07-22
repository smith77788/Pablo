"""Нарратив-кампании: pause/resume выведены в UI (движок умел, роутов/кнопок не было).

Класс «пауза без возобновления» + «мёртвая возможность движка». narrative_engine имел
pause_campaign/resume_campaign, но /narrative/{id}/pause|resume и кнопок не было.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api, narrative_engine

SRC = inspect.getsource(mini_app_api)
HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_engine_has_pause_resume():
    assert hasattr(narrative_engine, "pause_campaign") and hasattr(narrative_engine, "resume_campaign")


def test_routes_registered():
    assert 'add_post("/api/miniapp/narrative/{campaign_id}/pause", narrative_campaign_pause)' in SRC
    assert 'add_post("/api/miniapp/narrative/{campaign_id}/resume", narrative_campaign_resume)' in SRC


def test_handlers_owner_scoped_via_engine():
    for name in ("narrative_campaign_pause", "narrative_campaign_resume"):
        m = re.search(r"async def " + name + r"\(request.*?\n(.*?)\n    async def ", SRC, re.DOTALL)
        assert m, name
        body = m.group(1)
        assert "uid" in body and "cid, uid" in body, f"{name} должен передавать owner_id движку"


def test_ui_shows_pause_and_resume():
    assert "pauseNarrative(${c.id})" in HTML and "resumeNarrative(${c.id})" in HTML
    assert "c.status==='paused'" in HTML  # paused → возобновить, иначе пауза
