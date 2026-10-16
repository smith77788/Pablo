from __future__ import annotations

import pytest
import pytest_asyncio
from services.strike_engine import (
    get_randomized_interval,
    is_strike_allowed,
    generate_strike_report,
)


@pytest.mark.asyncio
async def test_get_randomized_interval_range():
    base = 60.0
    result = await get_randomized_interval(base)
    assert isinstance(result, float)
    assert base * 0.7 <= result <= base * 1.3


@pytest.mark.asyncio
async def test_is_strike_allowed_returns_bool():
    class FakePool:
        async def fetchrow(self, *args, **kwargs):
            return None

    result = await is_strike_allowed(FakePool(), target_id=1)
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_generate_strike_report_returns_dict():
    class FakePool:
        async def fetch(self, *args, **kwargs):
            return []

        async def fetchrow(self, *args, **kwargs):
            return None

    result = await generate_strike_report(FakePool(), operation_id=1)
    assert isinstance(result, dict)
    assert "operation_id" in result
    assert "total_actions" in result
    assert "success_rate" in result
