"""Регресс: централизованный тариф-гейт операций в operation_bus.submit().

min_plan операции объявлялся в OP_REGISTRY, но проверялся только в хендлерах —
забытый гейт = утечка платной масс-операции free-юзеру. Теперь submit() сам
проверяет min_plan (fail-open при сбое проверки, обход через bypass_plan_check).
"""

from __future__ import annotations

import pytest

import bot.utils.subscription as sub
from services import operation_bus


class FakePool:
    def __init__(self):
        self.inserted = False

    async def fetchrow(self, *args, **kwargs):
        self.inserted = True
        return {"id": 777}


@pytest.fixture
def paid_op():
    # берём реальную платную операцию из реестра
    for op, meta in operation_bus.OP_REGISTRY.items():
        mp = meta.get("min_plan")
        if mp and sub.coerce_plan(mp) == "paid":
            return op, meta
    pytest.skip("нет платной операции в OP_REGISTRY")


@pytest.mark.asyncio
async def test_enforce_blocks_underplan(monkeypatch, paid_op):
    op, meta = paid_op
    async def deny(pool, uid, plan):
        return False
    monkeypatch.setattr(sub, "require_plan", deny)
    with pytest.raises(operation_bus.PlanRequiredError):
        await operation_bus._enforce_min_plan(FakePool(), 1, op, meta)


@pytest.mark.asyncio
async def test_enforce_allows_paid(monkeypatch, paid_op):
    op, meta = paid_op
    async def allow(pool, uid, plan):
        return True
    monkeypatch.setattr(sub, "require_plan", allow)
    await operation_bus._enforce_min_plan(FakePool(), 1, op, meta)  # no raise


@pytest.mark.asyncio
async def test_enforce_no_min_plan_is_open():
    await operation_bus._enforce_min_plan(FakePool(), 1, "x", {})  # no min_plan → allow


@pytest.mark.asyncio
async def test_enforce_free_min_plan_is_open():
    await operation_bus._enforce_min_plan(FakePool(), 1, "x", {"min_plan": "free"})


@pytest.mark.asyncio
async def test_enforce_fail_open_on_check_error(monkeypatch, paid_op):
    op, meta = paid_op
    async def boom(pool, uid, plan):
        raise RuntimeError("db down")
    monkeypatch.setattr(sub, "require_plan", boom)
    # не должно поднять — fail-open
    await operation_bus._enforce_min_plan(FakePool(), 1, op, meta)


@pytest.mark.asyncio
async def test_submit_blocks_free_before_insert(monkeypatch, paid_op):
    op, _ = paid_op
    async def deny(pool, uid, plan):
        return False
    monkeypatch.setattr(sub, "require_plan", deny)
    pool = FakePool()
    with pytest.raises(operation_bus.PlanRequiredError):
        await operation_bus.submit(pool, 1, op, {})
    assert pool.inserted is False  # гейт сработал ДО вставки в очередь


@pytest.mark.asyncio
async def test_submit_allows_paid_and_enqueues(monkeypatch, paid_op):
    op, _ = paid_op
    async def allow(pool, uid, plan):
        return True
    monkeypatch.setattr(sub, "require_plan", allow)
    pool = FakePool()
    op_id = await operation_bus.submit(pool, 1, op, {})
    assert op_id == 777 and pool.inserted is True


@pytest.mark.asyncio
async def test_submit_bypass_skips_gate(monkeypatch, paid_op):
    op, _ = paid_op
    async def deny(pool, uid, plan):
        return False
    monkeypatch.setattr(sub, "require_plan", deny)
    pool = FakePool()
    op_id = await operation_bus.submit(pool, 1, op, {}, bypass_plan_check=True)
    assert op_id == 777  # обход гейта для доверенных внутренних вызовов
