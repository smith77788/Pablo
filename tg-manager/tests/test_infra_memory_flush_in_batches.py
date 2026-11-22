"""Память инфраструктуры пишется пачкой, и прирост во время записи не теряется.

`flush_to_db` раз в минуту делал по запросу на каждую пару «аккаунт ×
действие»: на широком флоте это тысячи запросов подряд на одном соединении
пула. Теперь — один `executemany` на кусок. Попутно закрыта потеря
счётчиков: отметка «записано» ставилась на значение ПОСЛЕ await, и успехи,
пришедшие во время записи, считались записанными, хотя в базу не уходили.
"""
from __future__ import annotations

import asyncio

import pytest

from services import infra_memory as im


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean():
    im._account_memory.clear()
    im._dirty_account_keys.clear()
    im._proxy_memory.clear()
    im._dirty_proxy_keys.clear()
    yield
    im._account_memory.clear()
    im._dirty_account_keys.clear()
    im._proxy_memory.clear()
    im._dirty_proxy_keys.clear()


class _Pool:
    def __init__(self, during=None, fail_batch=False, bad=None):
        self.batches, self.singles = [], []
        self.during, self.fail_batch, self.bad = during, fail_batch, bad

    async def executemany(self, q, rows):
        if self.during:
            self.during()
            self.during = None
        if self.fail_batch:
            raise RuntimeError("boom")
        self.batches.append((q, list(rows)))

    async def execute(self, q, *a):
        if self.bad is not None and a[0] == self.bad:
            raise RuntimeError("bad row")
        self.singles.append(a)


def test_one_batch_per_table():
    for i in range(300):
        im.record_account_op(i, "invite", True)
    im.record_proxy_op("socks5://u:p@h:1", "invite", True)
    pool = _Pool()
    _run(im.flush_to_db(pool))
    assert len(pool.batches) == 2 and not pool.singles
    assert len(pool.batches[0][1]) == 300
    assert not im._dirty_account_keys


def test_increment_during_write_is_not_lost():
    im.record_account_op(1, "invite", True)
    pool = _Pool(during=lambda: im.record_account_op(1, "invite", True))
    _run(im.flush_to_db(pool))
    assert pool.batches[0][1][0][2] == 1
    pool2 = _Pool()
    _run(im.flush_to_db(pool2))
    assert pool2.batches and pool2.batches[0][1][0][2] == 1, (
        "успех, пришедший во время записи, потерян")


def test_one_bad_row_does_not_hold_the_rest():
    for i in (1, 2, 3):
        im.record_account_op(i, "invite", True)
    pool = _Pool(fail_batch=True, bad=2)
    _run(im.flush_to_db(pool))
    assert sorted(a[0] for a in pool.singles) == [1, 3]
    assert im._dirty_account_keys == {(2, "invite")}
    assert im._account_memory[(1, "invite")].flushed_successes == 1
    assert im._account_memory[(2, "invite")].flushed_successes == 0
