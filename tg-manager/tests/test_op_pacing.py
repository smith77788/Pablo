"""Полное покрытие адаптивного темпа (services/op_pacing.py).

Расчёт задержки: множители по часу/дню недели + ML-множитель, джиттер, батч-фактор.
Плюс проверка, что op_worker сохранил контракт (re-export).
"""
from __future__ import annotations

from services import op_pacing as op


def test_delay_is_positive_and_scales_with_base():
    d1 = op.get_adaptive_delay(10.0, tz_offset=0)
    d2 = op.get_adaptive_delay(100.0, tz_offset=0)
    assert d1 > 0 and d2 > 0
    assert d2 > d1                      # больше базы — больше задержка


def test_multiplier_tables_have_expected_shape():
    assert len(op._HOUR_MULTIPLIER) == 24
    assert len(op._DAY_MULTIPLIER) == 7
    # ночь строже дня
    assert op._HOUR_MULTIPLIER[3] > op._HOUR_MULTIPLIER[12]
    # выходные строже будней
    assert op._DAY_MULTIPLIER[6] > op._DAY_MULTIPLIER[2]


def test_night_slows_more_than_midday(monkeypatch):
    # убираем джиттер и ML, чтобы сравнивать только временные множители
    monkeypatch.setattr(op.random, "uniform", lambda a, b: 1.0)

    class _Eng:
        def get_multiplier(self, action_type):
            return 1.0
    monkeypatch.setattr(op, "get_pacing_engine", lambda: _Eng())

    import datetime as dt

    class _FixedNight(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 1, 6, 3, 0, tzinfo=tz)   # 03:00 вторник

    class _FixedNoon(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 1, 6, 12, 0, tzinfo=tz)  # 12:00 вторник

    monkeypatch.setattr(op._dt, "datetime", _FixedNight)
    night = op.get_adaptive_delay(10.0, tz_offset=0)
    monkeypatch.setattr(op._dt, "datetime", _FixedNoon)
    noon = op.get_adaptive_delay(10.0, tz_offset=0)
    assert night > noon


def test_batch_delay_grows_with_batch_size(monkeypatch):
    monkeypatch.setattr(op.random, "uniform", lambda a, b: 1.0)

    class _Eng:
        def get_multiplier(self, action_type):
            return 1.0
    monkeypatch.setattr(op, "get_pacing_engine", lambda: _Eng())

    small = op.get_adaptive_batch_delay(10.0, batch_size=10, tz_offset=0)
    large = op.get_adaptive_batch_delay(10.0, batch_size=200, tz_offset=0)
    assert large > small


def test_op_worker_reexports_contract():
    from services import op_worker
    assert op_worker.get_adaptive_delay is op.get_adaptive_delay
    assert op_worker.get_adaptive_batch_delay is op.get_adaptive_batch_delay
    assert op_worker._HOUR_MULTIPLIER is op._HOUR_MULTIPLIER
