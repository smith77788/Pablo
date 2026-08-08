"""Регрессия: рабочие часы авто-ответа (_within_active_window).

Правило с окном рабочих часов молчит вне окна. Поддержка окна через полночь.
NULL с любой стороны = круглосуточно.
"""
from __future__ import annotations

from services.auto_responder import _within_active_window


def test_none_bounds_always_active():
    assert _within_active_window(3, None, None) is True
    assert _within_active_window(3, 9, None) is True
    assert _within_active_window(3, None, 18) is True


def test_daytime_window():
    # 9..18 → активно 9..17, молчит вне
    assert _within_active_window(9, 9, 18) is True
    assert _within_active_window(17, 9, 18) is True
    assert _within_active_window(18, 9, 18) is False
    assert _within_active_window(8, 9, 18) is False
    assert _within_active_window(0, 9, 18) is False


def test_overnight_window():
    # 22..6 → активно 22,23,0..5, молчит 6..21
    assert _within_active_window(22, 22, 6) is True
    assert _within_active_window(23, 22, 6) is True
    assert _within_active_window(0, 22, 6) is True
    assert _within_active_window(5, 22, 6) is True
    assert _within_active_window(6, 22, 6) is False
    assert _within_active_window(12, 22, 6) is False


def test_degenerate_equal_window_is_always():
    assert _within_active_window(0, 12, 12) is True
    assert _within_active_window(15, 12, 12) is True


def test_bad_values_fail_open():
    assert _within_active_window(10, "x", 5) is True
    assert _within_active_window(10, 9, "y") is True
