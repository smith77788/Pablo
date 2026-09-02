"""Идемпотентность постановки операции (operation_bus.submit).

Двойной тап кнопки / retry после таймаута не должны плодить дубль-операцию.
submit() — единый choke point (десятки вызовов), поэтому дедуп проверяем прямо
здесь: при наличии ещё-в-полёте идентичной операции возвращается её op_id без
второго INSERT; иначе — обычная вставка; окно 0 отключает поиск дубля.
"""
from __future__ import annotations

import pytest

from services import operation_bus as ob

OP = "bulk_join"  # реальный op_type из OP_REGISTRY


class _ACtx:
    """async-контекст, отдающий заданное значение (для acquire()/transaction())."""

    def __init__(self, val=None):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, existing=None, new_id=777):
        self.existing = existing
        self.new_id = new_id
        self.inserted = False
        self.fetchval_called = False
        self.advisory_locked = False

    def transaction(self):
        return _ACtx(None)

    async def execute(self, q, *a):
        if "pg_advisory_xact_lock" in q:
            self.advisory_locked = True

    async def fetchval(self, q, *a):
        self.fetchval_called = True
        return self.existing

    async def fetchrow(self, q, *a):
        self.inserted = True
        return {"id": self.new_id}


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _ACtx(self._conn)


@pytest.mark.asyncio
async def test_first_submit_inserts():
    conn = FakeConn(existing=None, new_id=101)
    pool = FakePool(conn)
    op_id = await ob.submit(pool, 42, OP, {"channel": "@x"}, bypass_plan_check=True)
    assert op_id == 101
    assert conn.inserted is True
    assert conn.advisory_locked is True  # сериализация включена


@pytest.mark.asyncio
async def test_duplicate_submit_reuses_without_insert():
    # прежняя идентичная операция ещё в полёте → fetchval вернёт её id
    conn = FakeConn(existing=555, new_id=999)
    pool = FakePool(conn)
    op_id = await ob.submit(pool, 42, OP, {"channel": "@x"}, bypass_plan_check=True)
    assert op_id == 555, "повторный сабмит должен вернуть существующий op_id"
    assert conn.inserted is False, "второго INSERT быть не должно"


@pytest.mark.asyncio
async def test_dedup_disabled_skips_lookup_and_inserts():
    conn = FakeConn(existing=555, new_id=333)
    pool = FakePool(conn)
    op_id = await ob.submit(
        pool, 42, OP, {"channel": "@x"}, bypass_plan_check=True, dedup_window_sec=0
    )
    assert op_id == 333, "при отключённом дедупе всегда новая операция"
    assert conn.fetchval_called is False, "поиск дубля не должен выполняться"
    assert conn.inserted is True


@pytest.mark.asyncio
async def test_unknown_op_type_rejected():
    conn = FakeConn()
    pool = FakePool(conn)
    with pytest.raises(ValueError):
        await ob.submit(pool, 42, "no_such_op", {}, bypass_plan_check=True)
    assert conn.inserted is False


def test_default_window_is_sane():
    assert 1 <= ob.DEFAULT_DEDUP_WINDOW_SEC <= 120
