"""Живая редакция обрабатывает свежие события Telegram ровно один раз."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from services import channel_admin as ca
from services import channel_admin_runner as runner
from services import va_references


class _Pool:
    def __init__(self, events=None):
        self.events = events or []
        self.fetchvals = [0, 0]
        self.executed = []
        self.conn = MagicMock()
        self.conn.execute = AsyncMock()

    async def fetchval(self, query, *args):
        return self.fetchvals.pop(0)

    async def fetch(self, query, *args):
        if "va_news_inbox" in query:
            return self.events
        return []

    async def fetchrow(self, *args):
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))

    @asynccontextmanager
    async def acquire(self):
        tx = AsyncMock()
        tx.__aenter__.return_value = None
        tx.__aexit__.return_value = False
        self.conn.transaction = MagicMock(return_value=tx)
        yield self.conn


def test_signal_hash_is_stable_for_case_and_spacing():
    assert va_references.news_signal_hash(" Новость   Дня! ") == va_references.news_signal_hash(
        "новость дня"
    )
    assert va_references.news_signal_hash("Другое событие") != va_references.news_signal_hash(
        "новость дня"
    )


@pytest.mark.asyncio
async def test_live_events_create_one_review_draft_and_are_consumed(monkeypatch):
    now = datetime.now(UTC)
    events = [{"id": 11, "source_username": "source", "source_message_id": 91,
               "published_at": now, "source_text": "Срочное решение принято"}]
    pool = _Pool(events)
    draft = ca.Draft("Срочные новости", "Текст", [ca._NEWS_REVIEW_REASON], False, False)
    monkeypatch.setattr(ca, "write_post", AsyncMock(return_value=draft))
    monkeypatch.setattr(ca, "save_draft", AsyncMock(return_value=44))
    monkeypatch.setattr(ca, "notify_draft", AsyncMock())
    monkeypatch.setattr(ca, "log_event", AsyncMock())

    created = await runner._draft_live_news(
        pool, None, {"owner_id": 7, "channel_id": 8, "posts_per_day": 3},
    )

    assert created is True
    assert ca.write_post.await_args.kwargs["news_events"] == events
    ca.save_draft.assert_awaited_once()
    update = pool.conn.execute.await_args
    assert "status='drafted'" in update.args[0]
    assert update.args[1:] == ([11], 44, 7, 8)
    assert any("@source" in reason for reason in draft.reasons)


@pytest.mark.asyncio
async def test_live_event_failure_returns_claim_for_bounded_retry(monkeypatch):
    now = datetime.now(UTC)
    pool = _Pool([{"id": 12, "source_username": "source", "source_message_id": 92,
                   "published_at": now, "source_text": "Сигнал"}])
    monkeypatch.setattr(ca, "write_post", AsyncMock(side_effect=RuntimeError("temporary")))

    assert await runner._draft_live_news(
        pool, None, {"owner_id": 7, "channel_id": 8, "posts_per_day": 3},
    ) is False
    query, args = pool.executed[-1]
    assert "attempts >= 3" in query and "status='drafting'" in query
    assert args == ([12],)


@pytest.mark.asyncio
async def test_scheduled_news_slot_waits_for_event_instead_of_writing_filler(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Свежие Новости Украина"}))
    write = AsyncMock()
    monkeypatch.setattr(ca, "write_post", write)
    admin = {"owner_id": 7, "channel_id": 8, "publish_mode": "auto", "posts_per_day": 2,
             "window_start": 9, "window_end": 21, "tz_offset": 3, "fail_streak": 0}

    result = await ca.tick_post(pool, None, admin, now=datetime.now(UTC))

    assert result == "news_waiting"
    write.assert_not_awaited()
    assert any("status='skipped'" in query for query, _ in pool.executed)


@pytest.mark.asyncio
async def test_news_channel_writes_intro_before_switching_to_event_mode(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Свежие Новости"}))
    write = AsyncMock(return_value=ca.Draft("Знакомство", "Приветствие", [], True, True))
    monkeypatch.setattr(ca, "write_post", write)
    monkeypatch.setattr(ca, "save_draft", AsyncMock(return_value=41))
    monkeypatch.setattr(ca, "notify_draft", AsyncMock())
    monkeypatch.setattr(ca, "log_event", AsyncMock())
    monkeypatch.setattr(ca, "_ok", AsyncMock())
    monkeypatch.setattr(ca, "_next_at", AsyncMock(return_value=datetime.now(UTC)))
    admin = {
        "owner_id": 7, "channel_id": 8, "publish_mode": "review", "posts_per_day": 2,
        "window_start": 9, "window_end": 21, "tz_offset": 3, "fail_streak": 0,
        "intro_pending": True,
    }

    result = await ca.tick_post(pool, None, admin, now=datetime.now(UTC))

    assert result == "draft"
    write.assert_awaited_once()
    assert not any("id=(SELECT id" in query for query, _ in pool.executed)


@pytest.mark.asyncio
async def test_news_scan_processes_bounded_batch_and_enqueues_events(monkeypatch):
    class ScanPool:
        def __init__(self):
            self.query = ""

        async def execute(self, *args):
            return "UPDATE 0"

        async def fetch(self, query, *args):
            self.query = query
            return [
                {"owner_id": 7, "channel_id": 8, "title": "Новости один"},
                {"owner_id": 7, "channel_id": 9, "title": "Новости два"},
            ]

    pool = ScanPool()
    prompts = AsyncMock(return_value=[])
    refresh = AsyncMock(return_value=[])
    drafts = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(va_references, "for_prompt", prompts)
    monkeypatch.setattr(va_references, "refresh_news_signals", refresh)
    monkeypatch.setattr(runner, "_draft_live_news", drafts)

    result = await runner._scan_live_news(pool, None)

    assert result == {"scanned": 2, "drafts": 1}
    assert "LIKE ANY" in pool.query and f"LIMIT {runner._NEWS_SCAN_BATCH}" in pool.query
    assert refresh.await_count == 2
    assert all(call.kwargs == {"force": True, "enqueue_events": True}
               for call in refresh.await_args_list)
