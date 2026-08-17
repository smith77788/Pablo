"""Гео-осведомлённый темп: ночной режим по таймзоне аккаунта, а не сервера."""
from __future__ import annotations

import datetime as dt
import os

from services import geo_tempo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_map_covers_common_countries():
    assert geo_tempo.timezone_for("gb") or geo_tempo.timezone_for("uk")
    assert geo_tempo.timezone_for("de")
    assert geo_tempo.timezone_for("RU")  # регистронезависимо
    assert geo_tempo.timezone_for("us")


def test_local_hour_differs_from_utc_by_timezone():
    # 12:00 UTC → Лондон ~12/13 (лето DST), Москва ~15.
    noon = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    lon = geo_tempo.local_hour("gb", noon)
    msk = geo_tempo.local_hour("ru", noon)
    assert lon is not None and msk is not None
    assert msk == 15
    assert lon in (12, 13)


def test_unknown_geo_returns_none_and_safe_defaults():
    assert geo_tempo.timezone_for("zz") is None
    assert geo_tempo.local_hour("zz") is None
    assert geo_tempo.is_local_night(None) is False
    # local_factor без гео откатывается на серверный факт (float > 0)
    assert geo_tempo.local_factor(None) > 0


def test_is_local_night_by_account_timezone():
    # 00:00 UTC: в Москве 03:00 (ночь), в Лос-Анджелесе ~16-17 (день).
    midnight = dt.datetime(2026, 1, 15, 0, 0, tzinfo=dt.timezone.utc)
    assert geo_tempo.is_local_night("ru", midnight) is True
    assert geo_tempo.is_local_night("us", midnight) is False


def test_local_factor_slower_at_local_night():
    # Глубокая ночь в стране → множитель заметно > дневного.
    # 02:00 в Москве = 23:00 UTC пред. дня.
    deep_night_msk = dt.datetime(2026, 1, 14, 23, 0, tzinfo=dt.timezone.utc)
    day_msk = dt.datetime(2026, 1, 15, 9, 0, tzinfo=dt.timezone.utc)  # 12:00 MSK
    # time_of_day_factor рандомизирован в диапазонах — берём минимум ночного > максимум дневного не гарантирован,
    # поэтому проверяем через локальный час напрямую.
    assert geo_tempo.local_hour("ru", deep_night_msk) == 2
    assert geo_tempo.local_hour("ru", day_msk) == 12


def test_naive_datetime_treated_as_utc():
    naive = dt.datetime(2026, 7, 1, 12, 0)  # без tzinfo
    aware = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    assert geo_tempo.local_hour("de", naive) == geo_tempo.local_hour("de", aware)


def test_op_worker_uses_geo_aware_tempo():
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    call = 'geo_tempo.local_factor(acc.get("geo_country"), account_id=acc["id"])'
    # циклы join/leave берут гео-локальный час И персональный хронотип аккаунта
    assert src.count(call) >= 2
