"""Отложенная рассылка переживает сбой связи с Telegram.

ЧТО БЫЛО. Перед запуском планировщик проверял токен бота через getMe и на
ЛЮБОМ неуспехе помечал расписание 'failed' навсегда — «токен отозван». Но
getMe отдаёт неуспех и на сетевой ошибке, 5xx и флуде. Одна минута сбоя
Telegram в момент запуска — и рассылка не уходила никогда, а у повторяемого
расписания вместе с ней обрывалась вся цепочка (следующее вхождение не
ставилось).

Теперь 'failed' — только на 401/404 (Telegram прямо сказал, что токена нет),
остальное возвращает расписание в очередь до следующего цикла.

И второе: отметка «пропущено» не затирает отмену владельца, случившуюся
после выборки, и не ставит следующее вхождение отменённой цепочки.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


class _Stop(Exception):
    pass


class _Pool:
    def __init__(self, status="pending"):
        self.status = status
        self.executed: list[str] = []

    async def execute(self, query, *args):
        self.executed.append(query)
        if "status='processing'" in query.split("WHERE")[0] and "status='pending'" in query:
            if self.status == "pending":
                self.status = "processing"
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET status='pending'" in query:
            if self.status == "processing":
                self.status = "pending"
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET status='missed'" in query:
            if "status='pending'" in query.split("WHERE")[1] and self.status != "pending":
                return "UPDATE 0"
            self.status = "missed"
            return "UPDATE 1"
        if "SET status='failed'" in query:
            self.status = "failed"
            return "UPDATE 1"
        return "UPDATE 1"


def _row(execute_at=None):
    return {"id": 7, "bot_id": 100, "token": "1:x", "message_text": "привет",
            "created_by": 1, "execute_at": execute_at, "repeat_interval_min": 60}


def _one_cycle(monkeypatch, pool, row, getme_reply, on_fetch=None):
    from services import bot_api, scheduler

    async def _noop(*a, **k):
        return {}

    async def _pending(p):
        if on_fetch:
            on_fetch()
        return [row]

    async def _call(http, token, method, **params):
        assert method == "getMe"
        return getme_reply

    reschedules: list[int] = []

    async def _resched(p, r):
        reschedules.append(r["id"])
        return 8

    async def _created(*a, **k):
        raise AssertionError("рассылка создана, хотя Telegram недоступен")

    async def _sleep(_s):
        raise _Stop

    monkeypatch.setattr(scheduler, "_ensure_claim_columns", _noop)
    monkeypatch.setattr(scheduler, "recover_stuck_claims", _noop)
    monkeypatch.setattr(scheduler.db, "get_pending_scheduled", _pending)
    monkeypatch.setattr(scheduler.db, "reschedule_if_recurring", _resched)
    monkeypatch.setattr(scheduler.db, "create_broadcast", _created)
    monkeypatch.setattr(bot_api, "_call", _call)
    monkeypatch.setattr(scheduler.asyncio, "sleep", _sleep)
    with pytest.raises(_Stop):
        asyncio.run(scheduler.run(pool, None))
    return reschedules


@pytest.mark.parametrize("reply", [
    {"ok": False, "error_code": 0, "description": "Network error after 3 retries"},
    {"ok": False, "error_code": 502, "description": "Bad Gateway"},
    {"ok": False, "error_code": 429, "description": "Too Many Requests",
     "parameters": {"retry_after": 120}},
])
def test_outage_returns_the_schedule_to_the_queue(monkeypatch, reply):
    pool = _Pool()
    _one_cycle(monkeypatch, pool, _row(), reply)
    assert pool.status == "pending", (
        f"сбой связи похоронил расписание: {pool.status}")


def test_revoked_token_still_fails_the_schedule(monkeypatch):
    pool = _Pool()
    _one_cycle(monkeypatch, pool, _row(),
               {"ok": False, "error_code": 401, "description": "Unauthorized"})
    assert pool.status == "failed"


def test_missed_mark_does_not_override_a_cancel(monkeypatch):
    pool = _Pool()
    late = datetime.now(timezone.utc) - timedelta(hours=3)

    def _owner_cancels():
        pool.status = "cancelled"

    resched = _one_cycle(monkeypatch, pool, _row(execute_at=late),
                         {"ok": True, "result": {}}, on_fetch=_owner_cancels)
    assert pool.status == "cancelled"
    assert resched == [], "отменённая цепочка получила следующее вхождение"
