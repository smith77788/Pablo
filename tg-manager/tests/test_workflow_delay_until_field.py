"""Задержка «до даты из поля»: напоминание о записи, дни рождения."""
from datetime import datetime, timezone

import pytest

from services import workflow_engine as wf
from services import workflow_runtime as rt

MSK = "Europe/Moscow"
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)  # 12:00 Москва


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_formats_people_write():
    assert rt.parse_field_datetime("2026-11-10 15:00", NOW, MSK) == _utc(2026, 11, 10, 12, 0)
    assert rt.parse_field_datetime("10.11.2026 15:00", NOW, MSK) == _utc(2026, 11, 10, 12, 0)
    assert rt.parse_field_datetime("2026-11-10T15:00", NOW, MSK) == _utc(2026, 11, 10, 12, 0)
    # Без времени — 10:00 по часам человека.
    assert rt.parse_field_datetime("10.11.2026", NOW, MSK) == _utc(2026, 11, 10, 7, 0)


def test_without_a_year_the_nearest_date_is_taken():
    assert rt.parse_field_datetime("10.11", NOW, MSK) == _utc(2026, 11, 10, 7, 0)
    assert rt.parse_field_datetime("01.03", NOW, MSK) == _utc(2027, 3, 1, 7, 0)
    # Сегодня — сегодня, а не через год.
    assert rt.parse_field_datetime("05.11 18:00", NOW, MSK) == _utc(2026, 11, 5, 15, 0)


def test_junk_is_not_a_date():
    for junk in ("", None, "завтра", "31.02.2026", "29.02", "2026-13-01"):
        assert rt.parse_field_datetime(junk, NOW, MSK) is None, junk


def test_the_step_is_validated():
    s = wf.validate_step({"type": "delay", "until_field": "Appointment", "offset_minutes": "-60"})
    assert s == {"type": "delay", "until_field": "appointment", "offset_minutes": -60}
    with pytest.raises(ValueError):
        wf.validate_step({"type": "delay", "until_field": "bad field"})
    with pytest.raises(ValueError):
        wf.validate_step({"type": "delay", "until_field": "x", "offset_minutes": 10 ** 7})
