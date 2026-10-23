"""Авточастота виртуального администратора: совет по числу постов в день.

Больше постов ≠ больше охвата. Советуем частоту по тренду охвата на пост (±1,
не выше потолка), и только как СОВЕТ владельцу в отчёте — сами не меняем.
Проверяем чистую логику порогов и отрисовку совета в дневном отчёте.
"""
from __future__ import annotations

from services import channel_admin as ca


def test_recommend_none_when_too_little_data():
    assert ca.recommend_frequency(2, 100.0, 200.0, sample=5) is None


def test_recommend_up_when_reach_per_post_rising():
    r = ca.recommend_frequency(2, 100.0, 130.0, sample=20)
    assert r and r["suggested"] == 3 and r["current"] == 2


def test_recommend_down_when_reach_per_post_falling():
    r = ca.recommend_frequency(3, 100.0, 70.0, sample=20)
    assert r and r["suggested"] == 2


def test_recommend_hold_when_flat():
    assert ca.recommend_frequency(2, 100.0, 100.0, sample=20) is None


def test_recommend_never_above_ceiling():
    assert ca.recommend_frequency(ca._FREQ_CEIL, 100.0, 300.0, sample=50) is None


def test_recommend_never_below_one():
    assert ca.recommend_frequency(1, 100.0, 10.0, sample=50) is None


def test_recommend_ignores_zero_history():
    assert ca.recommend_frequency(2, 0.0, 200.0, sample=20) is None


def test_plural_posts():
    assert ca.plural_posts(1) == "пост"
    assert ca.plural_posts(2) == "поста"
    assert ca.plural_posts(5) == "постов"
    assert ca.plural_posts(11) == "постов"
    assert ca.plural_posts(21) == "пост"


def test_daily_report_renders_frequency_advice():
    item = ("Канал", {"posts_7d": 10, "avg_views_7d": 500, "members": 1000,
                       "members_delta_7d": 50, "best_pillar": "польза",
                       "freq_suggestion": {"suggested": 3, "current": 2,
                                           "reason": "охват на пост растёт — аудитория готова читать чаще"}})
    out = ca.format_daily_report([item])
    assert "совет по частоте: 3 поста в день" in out
    assert "сейчас 2" in out


def test_daily_report_without_frequency_advice_has_no_line():
    item = ("Канал", {"posts_7d": 10, "avg_views_7d": 500, "members": 1000,
                       "members_delta_7d": 50, "best_pillar": "польза",
                       "freq_suggestion": None})
    assert "совет по частоте" not in ca.format_daily_report([item])
