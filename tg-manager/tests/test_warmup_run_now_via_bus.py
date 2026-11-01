"""«▶️ Запустить сейчас» прогрева идёт операцией шины, а не фоновой задачей.

ЧТО БЫЛО. `cb_warmup_run_now` и `cb_warmup_run_one` в боте звали
`run_daily_warmup` через `asyncio.create_task`: у дня прогрева не было
операции — ни истории в /ops, ни итога, ни отмены; рестарт процесса посреди
дня терял его молча, а кнопка обещала «⚡ Активные задачи», которых в
мини-аппе нет. Теперь кнопки ставят `account_warmup` с `run_now`, и
исполнитель (`op_worker._warmup_day_now`) возвращает честный итог: день,
который движок пропустил, — отказ с причиной ЭТОГО запуска.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from services import op_worker

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(c):
    return asyncio.run(c)


def test_bot_does_not_spawn_warmup_day_outside_bus():
    src = (ROOT / "bot" / "handlers" / "account_warmup.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"create_task\(\s*(run_daily_warmup|_run_all)", code)
    assert '"run_now": True' in code


class _Pool:
    def __init__(self, plan, skip_reason=None):
        self.plan, self.skip_reason = plan, skip_reason

    async def fetchrow(self, sql, *a):
        return self.plan if "account_warmup_plans" in sql else None

    async def fetchval(self, sql, *a):
        if "now()" in sql:
            return "T0"
        if "last_skip_reason" in sql:
            return self.skip_reason
        return None


_PLAN = {"id": 7, "account_id": 3, "owner_id": 1, "current_day": 2,
         "daily_actions": 10, "status": "active"}


def test_skipped_day_is_a_failure_with_reason(monkeypatch):
    from services import account_warmer

    async def _skip(pool, plan):
        return {"actions_done": 0, "actions_ok": 0, "completed": False}

    monkeypatch.setattr(account_warmer, "run_daily_warmup", _skip)
    res = _run(op_worker._warmup_day_now(_Pool(_PLAN, "no_subscription"), 1, 7, "Аня"))
    assert res["status"] == "failed"
    assert "не выполнен" in res["summary"]
    assert "no_subscription" not in res["summary"]   # причина по-русски


def test_done_day_reports_actions(monkeypatch):
    from services import account_warmer

    async def _ok(pool, plan):
        assert plan["id"] == 7
        return {"actions_done": 10, "actions_ok": 9, "completed": False}

    monkeypatch.setattr(account_warmer, "run_daily_warmup", _ok)
    res = _run(op_worker._warmup_day_now(_Pool(_PLAN), 1, 7, "Аня"))
    assert res["status"] == "done"
    assert "9 из 10" in res["summary"]


def test_no_active_plan_is_not_restarted(monkeypatch):
    from services import account_warmer

    async def _never(*a, **k):
        raise AssertionError("день не должен запускаться без активного плана")

    monkeypatch.setattr(account_warmer, "run_daily_warmup", _never)
    res = _run(op_worker._warmup_day_now(_Pool(None), 1, 7, "Аня"))
    assert res["status"] == "failed"
