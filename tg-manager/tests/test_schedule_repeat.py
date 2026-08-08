"""Регрессия: маппинг повтора расписания (одноразовое → повторяемое).

Отложенные рассылки раньше были только one-shot. schedule_repeat_minutes —
единый маппинг none/daily/weekly → минуты, используемый create_schedule;
планировщик по этому интервалу создаёт следующее вхождение.
"""
from __future__ import annotations

from services.mini_app_api import schedule_repeat_minutes


def test_none_is_zero():
    assert schedule_repeat_minutes("none") == 0
    assert schedule_repeat_minutes(None) == 0
    assert schedule_repeat_minutes("") == 0


def test_daily():
    assert schedule_repeat_minutes("daily") == 1440


def test_weekly():
    assert schedule_repeat_minutes("weekly") == 10080


def test_case_insensitive():
    assert schedule_repeat_minutes("DAILY") == 1440
    assert schedule_repeat_minutes("Weekly") == 10080


def test_unknown_falls_back_to_oneshot():
    assert schedule_repeat_minutes("hourly") == 0
    assert schedule_repeat_minutes("garbage") == 0
