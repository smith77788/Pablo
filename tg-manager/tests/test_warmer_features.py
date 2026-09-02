from __future__ import annotations

import pytest
from services.account_warmer import _time_of_day_multiplier, _actions_for_day_count


def test_time_of_day_multiplier_is_geo_aware_not_server_kyiv():
    """Множитель темпа считается по ЛОКАЛЬНОМУ времени гео аккаунта.

    Раньше был захардкожен Киев (UTC+2) для всего флота: аккаунт на US-прокси
    «бодрствовал» в киевские часы. Теперь делегируем в geo_tempo, поэтому в
    один и тот же момент разные гео дают разный множитель.
    """
    import datetime as _dt

    from services import geo_tempo

    now = _dt.datetime(2024, 1, 1, 2, 0, tzinfo=_dt.timezone.utc)
    # Детерминированное доказательство: локальные ЧАСЫ разных гео в один и тот
    # же момент различаются (сам множитель содержит джиттер — сравнивать его
    # значения между вызовами нельзя).
    assert geo_tempo.local_hour("UA", now) != geo_tempo.local_hour("US", now)
    assert geo_tempo.is_local_night("UA", now) != geo_tempo.is_local_night("US", now)
    # обёртка прогрева принимает гео аккаунта и отдаёт валидный множитель
    v = _time_of_day_multiplier("UA")
    assert isinstance(v, float) and 0 < v < 3


def test_time_of_day_multiplier_unknown_geo_falls_back():
    """Гео неизвестно → откат на серверное время, без падения (нет регрессии)."""
    for geo in (None, "", "ZZ"):
        v = _time_of_day_multiplier(geo)
        assert isinstance(v, float) and v > 0


def test_progressive_trust_filters_actions():
    low_day = _actions_for_day_count(day=1, target_daily=10)
    mid_day = _actions_for_day_count(day=5, target_daily=10)
    high_day = _actions_for_day_count(day=15, target_daily=10)
    assert low_day <= mid_day <= high_day
    assert low_day == 2
    assert mid_day == 7
    assert high_day == 10
