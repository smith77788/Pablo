"""Автономный режим вирт.администратора публикует сам — и новости тоже.

Жалоба владельца: «в некоторых каналах виртуальный администратор перестал
постить и лишь присылает на проверку, хотя в настройках автономная работа и
публикация без подтверждения».

Корень: новостные каналы (детекция по маркерам «новост/news/сводк/…») ВСЕГДА
слали черновик на проверку (фактчек), игнорируя publish_mode='auto'. По ТЗ
владельца: включён авто-режим → публикует сам; «на проверку» — только когда
авто выключен (review).
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from services import channel_admin as ca
from services import channel_admin_runner as runner


# ── Структура write_post ──────────────────────────────────────────────────────

def test_write_post_has_autonomous_flag():
    sig = inspect.signature(ca.write_post)
    assert "autonomous" in sig.parameters, "у write_post нет флага autonomous"
    src = inspect.getsource(ca.write_post)
    # новостная метка не форсится в автономном режиме
    assert "not autonomous" in src, (
        "write_post форсит новостную проверку даже в авто-режиме")


# ── _draft_live_news: auto публикует, review — на проверку ────────────────────

class _Conn:
    def __init__(self):
        self.executed = []

    async def execute(self, q, *a):
        self.executed.append((q, a))

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()


class _Pool:
    def __init__(self, events):
        self.events = events
        self.fetchvals = [0, 0]  # pending=0, published=0
        self.executed = []
        self.conn = _Conn()

    async def fetchval(self, q, *a):
        return self.fetchvals.pop(0)

    async def fetch(self, q, *a):
        return self.events if "va_news_inbox" in q else []

    async def fetchrow(self, *a):
        return None

    async def execute(self, q, *a):
        self.executed.append((q, a))

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _events():
    now = datetime.now(UTC)
    return [{"id": 21, "source_username": "src", "source_message_id": 5,
             "published_at": now, "source_text": "Новость"}]


def _patch(monkeypatch, calls, clean_draft: bool):
    draft = ca.Draft("Новости", "Текст поста",
                     [] if clean_draft else ["оборвано"], False, clean_draft)

    async def write_post(*a, **k):
        calls["autonomous"] = k.get("autonomous")
        return draft

    async def publish(*a, **k):
        calls["published"] = True
        return 777

    async def save_draft(*a):
        calls["saved"] = True
        return 1

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ca, "write_post", write_post)
    monkeypatch.setattr(ca, "publish", publish)
    monkeypatch.setattr(ca, "save_draft", save_draft)
    monkeypatch.setattr(ca, "notify_draft", noop)
    monkeypatch.setattr(ca, "log_event", noop)


@pytest.mark.asyncio
async def test_auto_mode_publishes_news(monkeypatch):
    calls = {}
    _patch(monkeypatch, calls, clean_draft=True)
    created = await asyncio.wait_for(runner._draft_live_news(
        _Pool(_events()), None,
        {"owner_id": 7, "channel_id": 8, "posts_per_day": 3, "publish_mode": "auto"}),
        timeout=2)
    assert created is True
    assert calls.get("autonomous") is True, "write_post не получил autonomous"
    assert calls.get("published") is True, "авто-режим не опубликовал новость"
    assert "saved" not in calls, "авто-режим ушёл на проверку вместо публикации"


@pytest.mark.asyncio
async def test_review_mode_still_sends_draft(monkeypatch):
    calls = {}
    _patch(monkeypatch, calls, clean_draft=True)
    created = await asyncio.wait_for(runner._draft_live_news(
        _Pool(_events()), None,
        {"owner_id": 7, "channel_id": 8, "posts_per_day": 3, "publish_mode": "review"}),
        timeout=2)
    assert created is True
    assert calls.get("published") is None, "режим проверки опубликовал без спроса"
    assert calls.get("saved") is True, "режим проверки не создал черновик"


@pytest.mark.asyncio
async def test_auto_mode_with_broken_draft_does_not_publish(monkeypatch):
    """Даже в авто-режиме брак (draft.ok=False) не публикуется — идёт на проверку."""
    calls = {}
    _patch(monkeypatch, calls, clean_draft=False)
    await asyncio.wait_for(runner._draft_live_news(
        _Pool(_events()), None,
        {"owner_id": 7, "channel_id": 8, "posts_per_day": 3, "publish_mode": "auto"}),
        timeout=2)
    assert calls.get("published") is None, "брак опубликован в авто-режиме"
    assert calls.get("saved") is True, "брак не ушёл на проверку"
