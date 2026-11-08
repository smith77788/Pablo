"""Утром — одно сообщение, а не два.

Утренний отчёт (services/weekly_report) и «несрочное одной сводкой за день»
(services/quiet_hours) оба приходили утром отдельными сообщениями. Теперь
отчёт забирает накопленное несрочное в себя, а отдельная сводка ждёт, пока
окно отчёта не закончится (на случай, если отчёт не дошёл).
"""
from __future__ import annotations

import asyncio
import datetime as dt

from database import db
from services import quiet_hours as qh
from services import weekly_report as wr
from services.organism import spine

UTC = dt.timezone.utc
TUE_9 = dt.datetime(2026, 11, 3, 9, 0, tzinfo=UTC)     # вторник, окно отчёта


class _Pool:
    def __init__(self, settings, held):
        self.settings, self.held, self.executed = settings, held, []

    async def fetchrow(self, sql, *a):
        return self.settings

    async def fetch(self, sql, *a):
        if "SELECT DISTINCT" in sql:
            return [{"held_uid": 7, **self.settings}] if self.held else []
        return list(self.held)

    async def execute(self, sql, *a):
        self.executed.append(sql.split()[0])
        if sql.startswith("DELETE"):
            self.held = [r for r in self.held if r["id"] > a[1]]


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append(text)


def _setup(monkeypatch, daily_on=True):
    settings = {"user_id": 7, "quiet_enabled": False, "quiet_start": 23, "quiet_end": 8,
                "tz": "UTC", "digest_only": True, "digest_hour": 8, "last_digest_at": None}
    held = [{"id": 1, "pref": "reports", "text": "<b>Отчёт канала</b>",
             "created_at": dt.datetime(2026, 11, 2, 20, 0)}]

    async def _state_get(pool, uid, key, default=None):
        return daily_on if key == wr.DAILY_ID else default

    async def _ok(*a):
        return True

    async def _daily(pool, uid):
        return "📊 <b>Итоги дня</b>"

    monkeypatch.setattr(spine, "state_get", _state_get)
    monkeypatch.setattr(db, "notify_dedup_ok", _ok)
    monkeypatch.setattr(wr, "build_daily", _daily)
    return _Pool(settings, held), _Bot()


def test_morning_report_carries_the_daily_digest(monkeypatch):
    pool, bot = _setup(monkeypatch)
    # Сводка своим ходом не уходит: её принесёт отчёт.
    assert asyncio.run(qh.flush_once(pool, bot, TUE_9)) == 0 and bot.sent == []
    assert asyncio.run(wr.maybe_send_daily(pool, bot, 7, now=TUE_9)) is True
    assert len(bot.sent) == 1
    msg = bot.sent[0]
    assert "Итоги дня" in msg and "Несрочные уведомления" in msg and "Отчёт канала" in msg
    assert pool.held == [] and pool.executed == ["DELETE", "UPDATE"]
    # Больше в это утро ничего не приходит.
    assert asyncio.run(qh.flush_once(pool, bot, TUE_9)) == 0 and len(bot.sent) == 1


def test_digest_goes_alone_when_there_is_no_morning_report(monkeypatch):
    pool, bot = _setup(monkeypatch, daily_on=False)
    assert asyncio.run(qh.flush_once(pool, bot, TUE_9)) == 1
    assert "Сводка за день" in bot.sent[0]


def test_digest_goes_alone_after_the_report_window(monkeypatch):
    # Отчёт не дошёл (или не влез) — сводка уходит сама после окна отчёта.
    pool, bot = _setup(monkeypatch)
    late = TUE_9.replace(hour=wr.window_end_hour())
    assert asyncio.run(qh.flush_once(pool, bot, late)) == 1


def test_too_long_report_is_not_merged(monkeypatch):
    pool, bot = _setup(monkeypatch)

    async def _long(pool, uid):
        return "x" * 3990

    monkeypatch.setattr(wr, "build_daily", _long)
    assert asyncio.run(wr.maybe_send_daily(pool, bot, 7, now=TUE_9)) is True
    assert "Несрочные" not in bot.sent[0] and len(pool.held) == 1
