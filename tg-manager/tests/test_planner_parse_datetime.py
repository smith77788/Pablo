"""Планировщик операций: разбор даты без года (регресс).

«05.01 10:00», введённое в декабре, бралось датой ТЕКУЩЕГО года — уже
прошедшей, и планировщик отвечал «время должно быть в будущем»: коротким
форматом запланировать на январь было нельзя.
"""
from __future__ import annotations

from datetime import datetime as _real_dt, timezone

import bot.handlers.botmother_menu as bm


class _FrozenDec(_real_dt):
    @classmethod
    def now(cls, tz=None):
        base = _real_dt(2026, 12, 28, 12, 0, tzinfo=timezone.utc)
        return base if tz else base.replace(tzinfo=None)


def test_date_without_year_rolls_to_next_year(monkeypatch):
    monkeypatch.setattr(bm, "datetime", _FrozenDec)
    dt = bm._parse_datetime("05.01 10:00")
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2027, 1, 5, 10)
    assert dt.tzinfo is not None


def test_date_without_year_this_year_kept(monkeypatch):
    monkeypatch.setattr(bm, "datetime", _FrozenDec)
    dt = bm._parse_datetime("30.12 09:15")
    assert (dt.year, dt.month, dt.day) == (2026, 12, 30)


def test_full_date_and_garbage(monkeypatch):
    monkeypatch.setattr(bm, "datetime", _FrozenDec)
    assert bm._parse_datetime("01.02.2027 08:00").year == 2027
    assert bm._parse_datetime("31.02 10:00") is None
    assert bm._parse_datetime("завтра") is None
