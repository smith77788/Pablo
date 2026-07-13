"""Паритет: бот-расписание рассылок поддерживает повтор (recurring).

Раньше db.create_scheduled не принимал repeat_interval_min → бот-рассылки были
одноразовыми, хотя reschedule_if_recurring уже умеет переочередять по интервалу
(эта фича была только в mini-app). Теперь бот даёт выбор периодичности.
"""
from __future__ import annotations

import os

import pytest

from database import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _RecPool:
    """Ловит INSERT scheduled_broadcasts и возвращает id, запоминая аргументы."""

    def __init__(self):
        self.last_args = None

    async def fetchval(self, query, *args):
        self.last_args = (query, args)
        return 555


@pytest.mark.asyncio
async def test_create_scheduled_persists_repeat_interval():
    pool = _RecPool()
    import datetime as dt
    when = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    sid = await db.create_scheduled(pool, bot_id=1, text="hi", execute_at=when,
                                    created_by=42, repeat_interval_min=1440)
    assert sid == 555
    query, args = pool.last_args
    assert "repeat_interval_min" in query
    # аргумент интервала реально долетает до INSERT (последний параметр)
    assert 1440 in args


@pytest.mark.asyncio
async def test_create_scheduled_defaults_to_one_shot():
    pool = _RecPool()
    import datetime as dt
    when = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    await db.create_scheduled(pool, 1, "hi", when, 42)  # без repeat → 0
    _, args = pool.last_args
    assert args[-1] == 0


@pytest.mark.asyncio
async def test_create_scheduled_sanitizes_bad_interval():
    pool = _RecPool()
    import datetime as dt
    when = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    await db.create_scheduled(pool, 1, "hi", when, 42, repeat_interval_min=-5)
    _, args = pool.last_args
    assert args[-1] == 0  # отрицательное → 0


def test_schedule_handler_offers_repeat_choices_and_threads_interval():
    h = _read("bot/handlers/schedule.py")
    assert "waiting_repeat" in h
    for act in ("rep_once", "rep_hourly", "rep_12h", "rep_daily", "rep_weekly"):
        assert act in h, f"нет варианта {act}"
    # выбранный интервал реально передаётся в create_scheduled
    assert "repeat_interval_min=interval" in h
    # интервалы осмысленные (сутки=1440, неделя=10080)
    assert "1440" in h and "10080" in h
