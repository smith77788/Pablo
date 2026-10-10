"""Сбой ДО исполнителя закрывает операцию так же полно, как любой другой.

ЧТО БЫЛО. У `_run_op_task` есть пролог: разбор params, чтение предохранителя,
ожидание семафора владельца. Исключение в прологе перехватывает обёртка
`_run_op_task_guarded`, и, если повтор не положен, она закрывала операцию одним
UPDATE: `status='failed'`, `error_msg` — и всё. Ни `result`, ни графика исходов,
ни события шины, ни подписи в аудит-трейле, ни слова владельцу.

Внутри ОДНОГО прогона сделанного здесь действительно нет. Но журнал целей
переживает возвраты в очередь: операция, уже отработавшая часть целей и
вернувшаяся после флуд-паузы или рестарта воркера, падает в прологе так же
легко, как любая другая, — и закрывалась ровным провалом без единой цифры.
Это тот же класс, что разобран у отмены, падения исполнителя, голодания по
флоту и бюджета живучести.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from services import op_status, op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _guard_body() -> str:
    src = _read("services/op_worker.py")
    start = src.index("async def _run_op_task_guarded(")
    m = re.search(r"\nasync def ", src[start + 10:])
    return src[start:start + 10 + m.start()]


class _Pool:
    def __init__(self, *, ok_n=0, failed_n=0, rows_updated=1):
        self.ok_n, self.failed_n = ok_n, failed_n
        self.rows_updated = rows_updated
        self.writes: list[tuple] = []

    async def fetchrow(self, query, *args):
        # Персистентный анти-повтор уведомлений: первая попытка всегда
        # проходит (в проде INSERT .. RETURNING отдаёт строку).
        if "notification_dedup" in query:
            return {"user_id": 555}
        if "operation_log" in query:
            return {"ok_n": self.ok_n, "failed_n": self.failed_n}
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.writes.append((query, args))
        return f"UPDATE {self.rows_updated}"

    def status_write(self):
        for query, args in self.writes:
            if "SET status=$3" in query:
                return query, args
        return None, None


class _Bot:
    pass


@pytest.fixture
def harness(monkeypatch):
    """Пролог валится, повтор не положен — остаётся только закрыть операцию."""
    seen: dict = {"metric": [], "compliance": [], "spine": [], "notified": []}

    from services import compliance_engine as _ce, metrics as _m
    from services.organism import spine as _spine

    def _inc(name, labels=None, value=1.0):
        seen["metric"].append((name, dict(labels or {})))
    monkeypatch.setattr(_m, "inc", _inc)
    monkeypatch.setattr(_m, "observe", lambda *a, **k: None)

    async def _record(pool, owner_id, acc_id, op_type, status, **kw):
        seen["compliance"].append((op_type, status, kw.get("op_id")))
    monkeypatch.setattr(_ce, "record", _record)

    async def _emit(pool, owner_id, kind, payload):
        seen["spine"].append((kind, payload))
    monkeypatch.setattr(_spine, "emit", _emit)

    async def _notify(pool, bot, owner_id, kind, text, **kw):
        seen["notified"].append((owner_id, text))
    monkeypatch.setattr(op_worker.db, "notify_if_enabled", _notify)

    async def _boom(pool, bot, row):
        raise RuntimeError("прокси владельца не читается")
    monkeypatch.setattr(op_worker, "_run_op_task", _boom)

    async def _no_retry(*a, **kw):
        return False
    monkeypatch.setattr(op_worker, "_maybe_requeue", _no_retry)
    return seen


def _fail_in_prologue(pool):
    row = {"id": 31, "owner_id": 555, "op_type": "mass_invite",
           "params": json.dumps({"account_ids": [1]})}

    async def _go():
        await op_worker._run_op_task_guarded(pool, _Bot(), row)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


# ── Взятые цели не исчезают ──────────────────────────────────────────────────

def test_work_from_a_previous_run_is_not_lost(harness):
    """Операция вернулась после флуд-паузы с 120 взятыми целями и упала в прологе."""
    pool = _Pool(ok_n=120)
    _fail_in_prologue(pool)
    _query, args = pool.status_write()
    assert args, "операция не закрыта вовсе — она осталась бы в 'running'"
    assert op_status.PARTIAL in args, (
        "120 уже отработанных целей закрыты ровным провалом"
    )


def test_a_first_run_failure_is_an_honest_failure(harness):
    pool = _Pool(ok_n=0)
    _fail_in_prologue(pool)
    _query, args = pool.status_write()
    assert op_status.FAILED in args


def test_result_is_written(harness):
    pool = _Pool(ok_n=120, failed_n=3)
    _fail_in_prologue(pool)
    _query, args = pool.status_write()
    payload = next((json.loads(a) for a in args
                    if isinstance(a, str) and a.startswith("{")), None)
    assert payload, "операция закрыта с result=NULL — показать нечего"
    assert (payload["ok"], payload["failed"]) == (120, 3)


def test_the_terminal_guard_stays(harness):
    pool = _Pool()
    _fail_in_prologue(pool)
    query, _args = pool.status_write()
    assert "status NOT IN" in query, "провал затрёт отмену владельца"
    assert "acct_wait_since=NULL" in query


# ── Исход виден снаружи ──────────────────────────────────────────────────────

def test_outcome_is_announced(harness):
    pool = _Pool(ok_n=120)
    _fail_in_prologue(pool)
    assert "infragram_operations_total" in [n for n, _ in harness["metric"]]
    assert "op_done" in [k for k, _ in harness["spine"]]
    assert harness["compliance"] == [("mass_invite", op_status.PARTIAL, 31)]


def test_owner_hears_about_it(harness):
    pool = _Pool(ok_n=120)
    _fail_in_prologue(pool)
    assert harness["notified"], "операция не запустилась и не сказала об этом"
    _owner, text = harness["notified"][0]
    assert "не запустилась" in text
    assert "120" in text, "владельцу не сказали, что часть целей уже отработана"


def test_a_cancelled_operation_is_not_reported_as_a_failure(harness):
    """Ноль обновлённых строк = владелец уже остановил операцию сам."""
    pool = _Pool(ok_n=5, rows_updated=0)
    _fail_in_prologue(pool)
    assert not harness["notified"]
    assert not harness["compliance"]


# ── Реестр активных освобождается всегда ─────────────────────────────────────

def test_the_active_registry_is_always_released(harness):
    """Утечка op_id здесь — максимальная цена: операция числится активной вечно."""
    pool = _Pool()
    _fail_in_prologue(pool)
    assert 31 not in op_worker._active_op_ids
    body = _guard_body()
    assert "finally:" in body and "_active_op_ids.discard(op_id)" in body
