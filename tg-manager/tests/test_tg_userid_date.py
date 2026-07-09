"""Регрессия: оценка даты регистрации Telegram-аккаунта по user_id.

Telegram не отдаёт дату регистрации — оцениваем интерполяцией по опорным точкам.
Тест фиксирует контракт: монотонность (больше id → не раньше по времени),
границы, валидный ISO-формат, устойчивость к мусору.
"""
from __future__ import annotations

import datetime as _dt

from services.tg_userid_date import estimate_registration_date, estimate_registration_ts


def test_monotonic_non_decreasing():
    ids = [1, 500_000, 5_000_000, 50_000_000, 150_000_000, 500_000_000,
           1_500_000_000, 3_000_000_000, 7_000_000_000, 9_000_000_000]
    ts = [estimate_registration_ts(i) for i in ids]
    assert all(a is not None for a in ts)
    assert all(ts[k] <= ts[k + 1] for k in range(len(ts) - 1)), ts


def test_invalid_inputs_return_none():
    for bad in (None, 0, -1, "", "abc"):
        assert estimate_registration_date(bad) is None
        assert estimate_registration_ts(bad) is None


def test_iso_date_format():
    d = estimate_registration_date(1_000_000_000)
    assert isinstance(d, str)
    # парсится как дата ISO
    _dt.date.fromisoformat(d)


def test_old_id_is_earlier_than_new_id():
    old = estimate_registration_date(2_000_000)      # старый аккаунт
    new = estimate_registration_date(7_000_000_000)  # новый аккаунт
    assert old < new  # строковое сравнение ISO-дат корректно по хронологии


def test_extrapolates_beyond_last_anchor():
    # очень большой id (новее последней опоры) всё равно даёт валидную дату
    d = estimate_registration_date(9_500_000_000)
    assert d and int(d[:4]) >= 2024
