"""Account Warmer (5B) — регрессия: потолок серии провалов прерывает прогон.

Классифицированные ошибки (fatal/restriction/flood) корректно прерывали прогрев,
но для НЕ-классифицированных провалов (timeout, generic, недоступный канал)
единственный счётчик consecutive_fails сбрасывался в ветке адаптивной паузы
(>=3 → пауза → reset) → потолка «прервать после серии провалов» не было, и
нездоровый аккаунт долбил Telegram весь дневной бюджет. Добавлен fail_streak
(сброс только на успехе) с порогом _WARMUP_MAX_FAIL_STREAK.
"""
from __future__ import annotations

from services import account_warmer as aw


def test_below_threshold_does_not_abort():
    for n in range(aw._WARMUP_MAX_FAIL_STREAK):
        assert aw._fail_streak_abort(n) is False


def test_at_threshold_aborts():
    assert aw._fail_streak_abort(aw._WARMUP_MAX_FAIL_STREAK) is True
    assert aw._fail_streak_abort(aw._WARMUP_MAX_FAIL_STREAK + 3) is True


def test_threshold_is_conservative():
    # Порог не должен быть слишком мал: день-0 аккаунт делает мало действий,
    # редкие benign-провалы (приватный канал/опрос) не должны рвать прогон.
    assert aw._WARMUP_MAX_FAIL_STREAK >= 5


def test_guard_wired_into_loop():
    import inspect

    src = inspect.getsource(aw._run_daily_warmup_impl)
    assert "_fail_streak_abort(fail_streak)" in src, "потолок серии провалов не подключён в цикл"
    assert "fail_streak = 0" in src  # сбрасывается на успехе
