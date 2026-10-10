"""«Назначение админов» говорит, почему не вышло, и не долбит безнадёжное.

Операция 238 (05.10.2026): «Успешно 9/51, ошибок 42» — без единой причины.
Отказ был голым False: нельзя было отличить «аккаунт не вступил в канал» от
«у канала кончились места админа», а на исчерпанном лимите промоутер
продолжал слать запросы за каждого оставшегося.
"""
from __future__ import annotations

import pytest

from services import account_manager
from services import op_worker


class _Pool:
    def __init__(self, n):
        self.n = n
        self.logs: list = []
        self.done: list = []

    async def fetch(self, q, *a):
        if "operation_log" in q:
            return [{"target": t} for t in self.done]
        return [{"id": i, "phone": f"+7{i}", "first_name": "", "tg_user_id": 100 + i}
                for i in range(2, self.n + 2)]

    async def fetchrow(self, q, *a):
        return {"status": "running"}

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        if "operation_log" in q:
            if "'error'" in q:
                self.logs.append(a[-1])
        return "UPDATE 1"


@pytest.fixture
def stand(monkeypatch):
    from database import db

    async def _owner(pool, acc_id, owner):
        return {"id": 1, "session_str": "s"}

    async def _yes(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    async def _zero(*a, **k):
        return 0.0

    async def _not_cancelled(*a, **k):
        return False
    monkeypatch.setattr(db, "get_account_for_telethon", _owner)
    monkeypatch.setattr(op_worker, "try_claim_account", _yes)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(op_worker, "_governed_delay", _zero)
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)

    def _run(outcomes, n, done=()):
        calls: list = []

        async def _ex(sess, ch, uid, **kw):
            calls.append(kw.get("pool"))
            return outcomes[len(calls) - 1]
        monkeypatch.setattr(account_manager, "promote_to_admin_ex", _ex)
        pool = _Pool(n)
        pool.done = list(done)
        import asyncio
        res = asyncio.run(op_worker._exec_promote_all_admins(
            pool, None, 7, 5, {"owner_acc_id": 1, "channel_id": 100}))
        return res, calls, pool
    return _run


def test_failure_reasons_are_named(stand):
    res, calls, pool = stand([(True, ""), (False, "not_participant"), (False, "not_participant")], 3)
    assert res["ok"] == 1 and res["fail"] == 2
    assert "не состоит в канале" in res["summary"] and ": 2" in res["summary"]
    assert len(pool.logs) == 2
    assert all(p is pool for p in calls), "пауза промоутера не дойдёт до пульса"


def test_admin_limit_stops_and_reports_the_rest(stand):
    res, calls, pool = stand([(True, ""), (False, "admins_too_much")] + [(True, "")] * 5, 7)
    assert len(calls) == 2, "на исчерпанном лимите промоутер продолжал слать запросы"
    assert "Не дошла очередь: 5" in res["summary"]
    assert "места админа" in res["summary"]


def test_repeat_does_not_promote_again(stand):
    res, calls, _ = stand([(True, "")] * 3, 3, done=["+72", "+73"])
    assert len(calls) == 1, "повтор снова промоутил тех, у кого права уже есть"
    assert res["ok"] == 3
