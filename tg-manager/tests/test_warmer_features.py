from __future__ import annotations

import pytest
from services.account_warmer import _time_of_day_multiplier, _actions_for_day_count


# Документированный диапазон session_simulator.time_of_day_factor: 0.75 в пик
# активности и до 5.0 в глубокую ночь (2–6 локальных). Прежняя граница `< 3`
# держалась только вне ночного окна: тест зависел от того, в котором часу его
# запустили, и падал примерно 26 раз из 30, когда в Киеве была ночь. Проверять
# надо КОНТРАКТ, а не то, что множитель сейчас маленький.
_TOD_MIN = 0.75
_TOD_MAX = 5.0


def test_time_of_day_multiplier_covers_its_whole_documented_range():
    """Множитель обязан укладываться в контракт в ЛЮБОЙ час, а не в тот, когда
    случился прогон тестов."""
    from services import session_simulator

    for hour in range(24):
        for _ in range(20):
            v = session_simulator.time_of_day_factor(hour)
            assert _TOD_MIN <= v <= _TOD_MAX, f"час {hour}: множитель {v} вне контракта"


def test_night_is_slower_than_peak():
    """Смысл множителя: ночью паузы длиннее. Если это перестанет быть так,
    поведенческая маскировка сломается молча."""
    from services import session_simulator

    night = max(session_simulator.time_of_day_factor(4) for _ in range(50))
    peak = min(session_simulator.time_of_day_factor(15) for _ in range(50))
    assert night > peak


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
    assert isinstance(v, float) and _TOD_MIN <= v <= _TOD_MAX


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
