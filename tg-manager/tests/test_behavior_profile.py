"""Поведенческий профиль на аккаунт: стабильный хронотип, десинхронизация флота."""
from __future__ import annotations

from services import behavior_profile
from services import session_simulator


def test_profile_is_stable_for_same_account():
    a = behavior_profile.profile(12345)
    b = behavior_profile.profile(12345)
    assert a == b


def test_profiles_differ_across_fleet():
    # На большом наборе аккаунтов встречаются разные хронотипы и сдвиги.
    shifts = {behavior_profile.profile(i)["hour_shift"] for i in range(1, 200)}
    chronos = {behavior_profile.profile(i)["chronotype"] for i in range(1, 200)}
    assert len(shifts) >= 3
    assert {"early", "late"} <= chronos


def test_profile_fields_in_range():
    for i in range(1, 300):
        p = behavior_profile.profile(i)
        assert -3 <= p["hour_shift"] <= 3
        assert 0.85 <= p["intensity"] <= 1.15
        assert p["chronotype"] in ("early", "normal", "late")


def test_tod_factor_shifts_personal_night():
    # Детерминированная кривая: множитель зависит от личного часа.
    calls = []

    def curve(hour):
        calls.append(hour)
        return 1.0

    # early-тип (shift -3): в локальные 05:00 личный час = 08:00.
    early_id = next(i for i in range(1, 500)
                    if behavior_profile.profile(i)["hour_shift"] == -3)
    behavior_profile.tod_factor(early_id, 5, curve)
    assert calls[-1] == (5 - (-3)) % 24 == 8


def test_tod_factor_none_hour_falls_back_but_keeps_intensity():
    p = behavior_profile.profile(777)
    got = behavior_profile.tod_factor(777, None, lambda h: 2.0)
    assert abs(got - 2.0 * p["intensity"]) < 1e-9


def test_desync_two_accounts_same_hour_differ():
    # Два аккаунта с разным сдвигом в один и тот же локальный час получают
    # РАЗНЫЙ личный час → флот не тормозит синхронно.
    seen = {}
    id_a = next(i for i in range(1, 500) if behavior_profile.profile(i)["hour_shift"] == -3)
    id_b = next(i for i in range(1, 500) if behavior_profile.profile(i)["hour_shift"] == 3)

    def curve(hour):
        return hour  # вернём личный час, чтобы сравнить

    ha = behavior_profile.tod_factor(id_a, 2, curve) / behavior_profile.profile(id_a)["intensity"]
    hb = behavior_profile.tod_factor(id_b, 2, curve) / behavior_profile.profile(id_b)["intensity"]
    assert ha != hb


def test_integrates_with_real_curve():
    # Не падает с реальной кривой session_simulator.
    v = behavior_profile.tod_factor(42, 3, session_simulator.time_of_day_factor)
    assert v > 0
