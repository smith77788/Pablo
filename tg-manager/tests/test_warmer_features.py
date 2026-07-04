from __future__ import annotations

import pytest
from services.account_warmer import _time_of_day_multiplier, _actions_for_day_count


def test_time_of_day_multiplier_night():
    from unittest.mock import patch
    from datetime import datetime, timedelta, timezone

    mock_time = datetime(2024, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
    with patch("services.account_warmer.datetime") as mock_dt:
        mock_dt.datetime.utcnow.return_value = mock_time
        mock_dt.timedelta = timedelta
        result = _time_of_day_multiplier()
        assert result == pytest.approx(1 / 3)


def test_time_of_day_multiplier_day():
    from unittest.mock import patch
    from datetime import datetime, timedelta, timezone

    mock_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    with patch("services.account_warmer.datetime") as mock_dt:
        mock_dt.datetime.utcnow.return_value = mock_time
        mock_dt.timedelta = timedelta
        result = _time_of_day_multiplier()
        assert result == pytest.approx(1.5)


def test_progressive_trust_filters_actions():
    low_day = _actions_for_day_count(day=1, target_daily=10)
    mid_day = _actions_for_day_count(day=5, target_daily=10)
    high_day = _actions_for_day_count(day=15, target_daily=10)
    assert low_day <= mid_day <= high_day
    assert low_day == 2
    assert mid_day == 7
    assert high_day == 10
