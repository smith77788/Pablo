"""Пиковые часы аудитории виртуального администратора.

Администратор учится, в какой час внутри окна владельца посты набирают больше
охвата, и притягивает слоты контент-плана к этим часам — не выходя за окно и не
меняя частоту. Проверяем чистую логику: бакеты по локальному часу, часовой пояс,
подтяжку редких часов к среднему, фильтр окна, и что next_slot со списком пиков
двигает слот в окно, монотонно и с откатом к прежнему поведению без данных.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from services import channel_admin as ca


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _posts(hour_utc, score, n):
    # n постов в один и тот же час UTC с заданной оценкой охвата
    return [(_utc(2026, 1, 1 + i // 20, hour_utc), float(score)) for i in range(n)]


def test_peak_hours_empty_when_too_little_data():
    posts = _posts(18, 1000, 5)  # < _PEAK_MIN_TOTAL
    assert ca.peak_hours(posts, 0, 8, 22) == []


def test_peak_hours_ranks_high_reach_hour_first():
    posts = _posts(18, 2000, 10) + _posts(10, 200, 10)
    ranked = ca.peak_hours(posts, 0, 8, 22)
    assert ranked[0] == 18
    assert 18 in ranked


def test_peak_hours_excludes_hours_outside_window():
    # Лучший по охвату час — 3:00, но он вне окна 8..22 → не предлагаем, и ночной
    # выброс не занижает базу дневных часов (база считается только по окну).
    posts = _posts(3, 5000, 14) + _posts(14, 2000, 14) + _posts(20, 200, 14)
    ranked = ca.peak_hours(posts, 0, 8, 22)
    assert 3 not in ranked
    assert ranked and ranked[0] == 14


def test_peak_hours_applies_tz_offset():
    # 15:00 UTC при смещении +3 — это локальные 18:00
    posts = _posts(15, 2000, 10) + _posts(7, 200, 10)  # 7 UTC → 10 локальных
    ranked = ca.peak_hours(posts, 3, 8, 22)
    assert ranked[0] == 18


def test_shrunk_mean_pulls_small_samples_toward_prior():
    # один пост с огромным охватом подтягивается к среднему, а не доминирует как есть
    pulled = ca._shrunk_mean(1, 10000.0, 100.0)
    assert 100.0 < pulled < 10000.0
    # много постов — почти своё среднее
    many = ca._shrunk_mean(100, 10000.0, 100.0)
    assert many > pulled


def test_next_slot_without_peaks_unchanged():
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    now = _utc(2026, 1, 1, 9)
    a = ca.next_slot(now, posts_per_day=3, window_start=8, window_end=22, tz_offset=0, rng=rng1)
    b = ca.next_slot(now, posts_per_day=3, window_start=8, window_end=22, tz_offset=0,
                     rng=rng2, peak_hours=None)
    assert a == b


def test_next_slot_snaps_into_window_and_stays_inside():
    now = _utc(2026, 1, 1, 9)
    for seed in range(20):
        s = ca.next_slot(now, posts_per_day=2, window_start=8, window_end=22, tz_offset=0,
                         rng=random.Random(seed), peak_hours=[14])
        assert 8 <= s.hour < 22, s


def test_next_slot_sequence_strictly_increasing_with_peaks():
    cursor = _utc(2026, 1, 1, 8)
    peaks = [10, 14, 18]
    prev = cursor
    for _ in range(40):
        cursor = ca.next_slot(cursor, posts_per_day=3, window_start=8, window_end=22,
                              tz_offset=0, rng=random.Random(7), peak_hours=peaks)
        assert cursor > prev, (prev, cursor)
        assert 8 <= (cursor + timedelta(0)).hour < 22
        prev = cursor


def test_snap_to_peak_leaves_far_peaks_alone():
    # пик далеко (> половины интервала) — слот не трогаем
    local = _utc(2026, 1, 1, 9)
    out = ca._snap_to_peak(local, [21], window_start=8, window_end=22,
                           interval_h=4.0, rng=random.Random(1))
    assert out.hour == 9  # 21 слишком далеко от 9 при reach=2ч


import pytest  # noqa: E402
from tests.test_executors import FakePool  # noqa: E402


@pytest.mark.asyncio
async def test_learn_peak_hours_reads_posts_and_ranks():
    rows = ([{"published_at": _utc(2026, 1, 1 + i, 18), "views": 2000,
              "reactions": 50, "forwards": 20} for i in range(8)]
            + [{"published_at": _utc(2026, 1, 1 + i, 11), "views": 150,
                "reactions": 1, "forwards": 0} for i in range(8)])
    pool = FakePool(fetch=rows)
    peaks = await ca.learn_peak_hours(pool, 42, 100, tz_offset=0, window_start=8, window_end=22)
    assert peaks and peaks[0] == 18


@pytest.mark.asyncio
async def test_learn_peak_hours_empty_without_data():
    pool = FakePool(fetch=[])
    assert await ca.learn_peak_hours(pool, 42, 100, tz_offset=0,
                                     window_start=8, window_end=22) == []
