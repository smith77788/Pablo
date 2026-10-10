"""Готовность ИИ видна владельцу: без него администратор молчит осмысленно.

Без ИИ виртуальный администратор «включён», настраивается по названию канала,
но каждый такт публикации падает на генерации поста и уходит в самолечение — в
канал ничего не выходит. Владелец видел только редкий алерт после трёх сбоев.
Проверка ai_ready() выносит причину на экран сразу.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import channel_admin as ca  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_ai_ready_true_when_claude_enabled(monkeypatch):
    import services.ai_claude as aic
    monkeypatch.setattr(aic, "enabled", lambda: True)
    ok, note = ca.ai_ready()
    assert ok is True and note == ""


def test_ai_ready_true_when_a_provider_is_configured(monkeypatch):
    import services.ai_claude as aic
    import services.ai_providers as aip
    monkeypatch.setattr(aic, "enabled", lambda: False)
    monkeypatch.setattr(aip, "configured_providers", lambda: [object()])
    ok, note = ca.ai_ready()
    assert ok is True


def test_ai_not_ready_gives_a_russian_reason(monkeypatch):
    import services.ai_claude as aic
    import services.ai_providers as aip
    monkeypatch.setattr(aic, "enabled", lambda: False)
    monkeypatch.setattr(aip, "configured_providers", lambda: [])
    ok, note = ca.ai_ready()
    assert ok is False
    assert note and "ИИ" in note
    # причина ведёт к действию, а не просто «ошибка»
    assert "подключите" in note.lower() or "подключ" in note.lower()


def test_ai_check_never_raises(monkeypatch):
    """Диагностика не должна ронять сводку, даже если сам детектор сломан."""
    import services.ai_claude as aic
    def _boom():
        raise RuntimeError("сломано")
    monkeypatch.setattr(aic, "enabled", _boom)
    import services.ai_providers as aip
    monkeypatch.setattr(aip, "configured_providers", _boom)
    ok, note = ca.ai_ready()  # не должно бросать
    assert ok is False and note


def test_overview_exposes_ai_readiness(monkeypatch):
    """network_overview отдаёт готовность ИИ — иначе UI её не покажет."""
    class _Pool:
        async def fetchrow(self, *a, **k): return None
        async def fetchval(self, *a, **k): return 0
        async def fetch(self, *a, **k): return []
    import services.ai_claude as aic
    monkeypatch.setattr(aic, "enabled", lambda: True)
    out = asyncio.run(ca.network_overview(_Pool(), 1))
    assert "ai_ready" in out and "ai_note" in out


def test_ui_shows_the_ai_banner():
    js = (ROOT / "mini_app/screens/va_admin.js").read_text(encoding="utf-8")
    assert "ai_ready === false" in js, "UI не показывает баннер об отсутствии ИИ"
    assert "d.network.ai_note" in js
