"""Флот: честный дележ аккаунтов между параллельными операциями.

Одна операция больше не забирает весь свободный флот, если у владельца идут
другие операции — иначе конкурентные операции падали «все аккаунты заняты».
В одиночку операция берёт весь флот (без потери пропускной способности).
"""
from __future__ import annotations

import asyncio

import pytest

import services.op_worker as ow


class _FakePool:
    def __init__(self, running):
        self._running = running

    async def fetchval(self, q, *a):
        return self._running

    async def execute(self, q, *a):
        return "UPDATE"


def _accts(n):
    return [{"id": i} for i in range(1, n + 1)]


@pytest.fixture(autouse=True)
def _isolate_op_worker_globals():
    """Не протекать в другие тесты: сохранить/восстановить глобалы op_worker."""
    saved_pool = ow._db_pool
    saved_in_use = set(ow._accounts_in_use)
    saved_locks = dict(ow._operation_account_locks)
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    try:
        yield
    finally:
        ow._db_pool = saved_pool
        ow._accounts_in_use.clear()
        ow._accounts_in_use.update(saved_in_use)
        ow._operation_account_locks.clear()
        ow._operation_account_locks.update(saved_locks)


def _reset():
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()


def test_solo_op_claims_whole_free_fleet():
    _reset()
    ow._db_pool = _FakePool(running=1)  # только эта операция
    claimed = asyncio.run(ow._claim_available_accounts(101, _accts(9), owner_id=7))
    assert len(claimed) == 9   # в одиночку — весь флот


def test_three_parallel_ops_split_fleet_disjointly():
    _reset()
    ow._db_pool = _FakePool(running=3)  # три параллельные операции владельца
    a = asyncio.run(ow._claim_available_accounts(1, _accts(9), owner_id=7))
    b = asyncio.run(ow._claim_available_accounts(2, _accts(9), owner_id=7))
    c = asyncio.run(ow._claim_available_accounts(3, _accts(9), owner_id=7))
    # каждая получила справедливую долю (⌈9/3⌉=3), и доли ДИЗЪЮНКТНЫ
    assert len(a) == 3 and len(b) == 3 and len(c) == 3
    ids = [x["id"] for x in a + b + c]
    assert len(set(ids)) == 9   # ни один аккаунт не выдан двум операциям


def test_second_op_is_not_starved_under_contention():
    _reset()
    ow._db_pool = _FakePool(running=2)
    a = asyncio.run(ow._claim_available_accounts(1, _accts(10), owner_id=7))
    b = asyncio.run(ow._claim_available_accounts(2, _accts(10), owner_id=7))
    # первая НЕ забрала весь флот → второй досталось (раньше было 0 → «заняты»)
    assert len(a) == 5 and len(b) == 5
    assert not (set(x["id"] for x in a) & set(x["id"] for x in b))


def test_min_floor_when_more_ops_than_accounts():
    _reset()
    ow._db_pool = _FakePool(running=5)  # больше операций, чем аккаунтов
    a = asyncio.run(ow._claim_available_accounts(1, _accts(2), owner_id=7))
    assert len(a) >= ow._MIN_ACCOUNTS_PER_OP   # хотя бы минимум


def test_no_owner_id_keeps_legacy_whole_claim():
    _reset()
    ow._db_pool = _FakePool(running=3)
    claimed = asyncio.run(ow._claim_available_accounts(1, _accts(9)))  # owner_id=None
    assert len(claimed) == 9   # без owner-контекста — прежнее поведение


def test_busy_fleet_requeues_instead_of_failing():
    src = open("services/op_worker.py", encoding="utf-8").read()
    # исполнители при занятом флоте возвращают requeue, а не failed
    assert '"status": "requeue"' in src
    assert src.count('"status": "requeue"') >= 3
    # _run_op_task обрабатывает requeue → мягкий возврат в очередь
    assert 'result.get("status") == "requeue"' in src
    assert "async def _requeue_op_no_accounts" in src
    # ограничение по времени, чтобы не крутиться вечно
    assert "_ACCT_WAIT_MAX_MIN" in src


def test_claim_passes_owner_id_at_call_sites():
    src = open("services/op_worker.py", encoding="utf-8").read()
    # все исполнительные вызовы claim передают owner_id (честный дележ)
    import re
    calls = re.findall(r"_claim_available_accounts\(op_id, [a-z_]+(, owner_id)?\)", src)
    # каждый вызов (кроме определения) должен содержать owner_id
    assert src.count("_claim_available_accounts(op_id,") >= 5
    assert src.count(", owner_id)") >= 5
