"""Регресс на единую активацию подписки (services/billing.py).

Фиксируем инварианты, из-за нарушения которых webhook и checker раньше
расходились: platform_users синкается ТЕМ ЖЕ expires_at, что вернул апсерт
подписки; план нормализуется из единого источника; не-подписочные планы
(free/strike) не активируются.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import billing


class FakeExecutor:
    """Мини-заглушка asyncpg: запоминает вызовы, отдаёт заданный expires."""

    def __init__(self, expires):
        self._expires = expires
        self.fetchval_calls: list[tuple] = []
        self.execute_calls: list[tuple] = []

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self._expires

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


EXP = datetime(2027, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_activation_syncs_platform_users_with_same_expiry():
    ex = FakeExecutor(EXP)
    got = await billing.activate_subscription(ex, user_id=42, plan="paid", months=3)
    assert got == EXP
    # ровно один апсерт подписки и один синк platform_users
    assert len(ex.fetchval_calls) == 1
    assert len(ex.execute_calls) == 1
    sync_query, sync_args = ex.execute_calls[0]
    assert "platform_users" in sync_query
    # синк использует ИМЕННО возвращённый expires (а не пересчитанную дату)
    assert sync_args == (42, "paid", EXP)


@pytest.mark.asyncio
async def test_activation_normalizes_alias_plan():
    ex = FakeExecutor(EXP)
    await billing.activate_subscription(ex, user_id=7, plan="pro", months=1)
    _, upsert_args = ex.fetchval_calls[0]
    assert upsert_args[1] == "paid"  # 'pro' -> 'paid'
    assert ex.execute_calls[0][1][1] == "paid"


@pytest.mark.asyncio
async def test_less_than_a_month_is_refused_not_rounded_up():
    """Срок 0 — подделанная кнопка (цена 0), а не «округлим до месяца».

    Раньше здесь закреплялось обратное: max(1, months) выдавал месяц за счёт
    на ноль (D1 в OPEN_DEFECTS.md).
    """
    for bad in (0, -1, None):
        ex = FakeExecutor(EXP)
        with pytest.raises(ValueError):
            await billing.activate_subscription(ex, user_id=7, plan="paid", months=bad)
        assert ex.fetchval_calls == [] and ex.execute_calls == []


@pytest.mark.asyncio
async def test_non_subscription_plans_do_nothing():
    for plan in ("free", "strike", "totally-unknown"):
        ex = FakeExecutor(EXP)
        got = await billing.activate_subscription(ex, user_id=1, plan=plan, months=1)
        assert got is None, plan
        assert ex.fetchval_calls == []
        assert ex.execute_calls == []


def test_is_subscription_plan():
    assert billing.is_subscription_plan("paid")
    assert billing.is_subscription_plan("pro")  # alias
    assert not billing.is_subscription_plan("free")
    assert not billing.is_subscription_plan("strike")
    assert not billing.is_subscription_plan(None)
