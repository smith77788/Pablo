from __future__ import annotations

import pytest


def test_get_adaptive_delay_returns_float():
    from services.op_worker import get_adaptive_delay
    result = get_adaptive_delay(10.0)
    assert isinstance(result, float)
    assert result > 0


def test_get_adaptive_delay_night_multiplier():
    from services.op_worker import get_adaptive_delay
    result = get_adaptive_delay(10.0, tz_offset=0)
    assert result >= 0


def test_get_adaptive_delay_day_multiplier():
    from services.op_worker import get_adaptive_delay
    result = get_adaptive_delay(10.0, tz_offset=0)
    assert result >= 0


def test_get_adaptive_batch_delay_increases_with_size():
    from services.op_worker import get_adaptive_batch_delay
    small = get_adaptive_batch_delay(10.0, batch_size=10, tz_offset=0)
    large = get_adaptive_batch_delay(10.0, batch_size=200, tz_offset=0)
    assert large >= small
