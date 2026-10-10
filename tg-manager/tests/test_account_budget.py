"""Тесты дневного бюджета действий на аккаунт (без реальной БД)."""
from __future__ import annotations

import pytest

from tests.test_executors import FakePool


@pytest.mark.asyncio
async def test_actions_today_bulk_shapes_result():
    from services import account_budget as ab
    # pool отдаёт агрегаты для 2 из 3 аккаунтов
    pool = FakePool(fetch=[{"account_id": 1, "n": 60}, {"account_id": 2, "n": 5}])
    counts = await ab.actions_today_bulk(pool, [1, 2, 3])
    assert counts == {1: 60, 2: 5, 3: 0}  # отсутствующий → 0


@pytest.mark.asyncio
async def test_filter_within_budget_splits():
    from services import account_budget as ab
    pool = FakePool(fetch=[{"account_id": 1, "n": 60}, {"account_id": 2, "n": 5}])
    within, over = await ab.filter_within_budget(pool, [1, 2, 3], limit=50)
    assert within == [2, 3]      # 5 и 0 < 50
    assert over == [1]           # 60 >= 50


@pytest.mark.asyncio
async def test_budget_disabled_passes_all():
    from services import account_budget as ab
    pool = FakePool(fetch=[])
    within, over = await ab.filter_within_budget(pool, [1, 2], limit=0)
    assert within == [1, 2]
    assert over == []


@pytest.mark.asyncio
async def test_budget_empty_input():
    from services import account_budget as ab
    within, over = await ab.filter_within_budget(FakePool(), [], limit=50)
    assert within == [] and over == []


@pytest.mark.asyncio
async def test_get_daily_budget_default():
    from services import account_budget as ab
    # get_platform_setting вернёт '' (fetchval=None) → дефолт
    pool = FakePool(fetchval=None)
    assert await ab.get_daily_budget(pool) == ab.DEFAULT_DAILY_BUDGET
