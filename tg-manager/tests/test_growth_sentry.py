"""Дозор роста — детектор накрутки подписчиков по ряду снимков.

Проверяем сердце: органичный (пологий) рост НЕ тревожит, вертикальный всплеск и
обвал — ловятся, а рамки (мало истории, крошечный канал, большой но органичный
приток) не дают ложных звонков. Плюс дедуп: один всплеск не звонит дважды.
"""
from __future__ import annotations

import asyncio
import json

from services import growth_sentry as G


# ── Чистый классификатор ─────────────────────────────────────────────────────

def test_organic_growth_is_calm():
    # Пологий рост ~+30/день на большом канале — обычный темп, не накрутка.
    series = [1000, 1030, 1058, 1090, 1121, 1150, 1182]
    assert G.classify_series(series)["level"] == G.CALM


def test_flat_channel_is_calm():
    assert G.classify_series([5000, 5001, 4999, 5002, 5000, 5003])["level"] == G.CALM


def test_vertical_spike_is_flagged():
    # обычный шаг ~30, затем +2000 за день на канале в 1150 — накрутка.
    series = [1000, 1030, 1060, 1090, 1120, 1150, 3150]
    v = G.classify_series(series)
    assert v["level"] == G.SPIKE
    assert v["delta"] == 2000
    assert v["at_index"] == 6


def test_crash_is_flagged():
    series = [5000, 5030, 5060, 5090, 5120, 5150, 1150]
    v = G.classify_series(series)
    assert v["level"] == G.CRASH
    assert v["delta"] == -4000


def test_small_channel_ignored():
    # тот же относительный всплеск, но канал крошечный (< ABS_FLOOR) — шум.
    series = [10, 12, 14, 16, 18, 20, 60]
    assert G.classify_series(series)["level"] == G.CALM


def test_short_history_is_calm():
    assert G.classify_series([1000, 5000])["level"] == G.CALM
    assert G.classify_series([])["level"] == G.CALM


def test_big_but_organic_absolute_growth_not_flagged():
    """Быстрорастущий канал: большой абсолютный приток, но РОВНЫЙ — не всплеск,
    т.к. нет кратного отрыва от собственного темпа."""
    series = [10000, 11000, 12000, 13000, 14000, 15000, 16000]
    assert G.classify_series(series)["level"] == G.CALM


def test_explain_is_russian_and_nonempty():
    v = G.classify_series([1000, 1030, 1060, 1090, 1120, 1150, 3150])
    txt = G.explain(v, "Мой канал")
    assert "накрутк" in txt.lower() and "Мой канал" in txt


# ── Обёртки над БД на заглушке пула ─────────────────────────────────────────

class _Pool:
    """Заглушка: каналы владельца + ряды истории по channel_id; organism_state
    в памяти (заглушка типы связывания не ловит — CLAUDE.md)."""

    def __init__(self, channels, series_by_cid, state=None):
        self._channels = channels                # [{"channel_id","title"}]
        self._series = series_by_cid             # {cid: [counts...]}
        self._state = state
        self.emitted = []

    async def fetch(self, q, *a):
        if "managed_channels" in q:
            return [dict(c) for c in self._channels]
        if "channel_member_history" in q:
            cid = a[1]
            return [{"captured_on": f"d{i}", "members_count": c}
                    for i, c in enumerate(self._series.get(cid, []))]
        return []

    async def fetchrow(self, q, *a):
        if "organism_state" in q:
            return {"value": self._state} if self._state is not None else None
        return None

    async def execute(self, q, *a):
        if "organism_state" in q:
            self._state = json.loads(a[2])
        elif "organism_events" in q:
            self.emitted.append(a)
        return "OK"


def test_summary_counts_suspicious_channels():
    pool = _Pool(
        channels=[{"channel_id": 1, "title": "Чистый"},
                  {"channel_id": 2, "title": "Накрученный"}],
        series_by_cid={
            1: [1000, 1030, 1060, 1090, 1120, 1150, 1180],       # органика
            2: [1000, 1030, 1060, 1090, 1120, 1150, 4150],       # всплеск
        })
    s = asyncio.run(G.summary(pool, owner_id=7))
    assert s["suspicious"] == 1
    assert s["fake_top"] == "Накрученный"


def test_scan_remembers_and_dedups():
    pool = _Pool(
        channels=[{"channel_id": 2, "title": "Накрученный"}],
        series_by_cid={2: [1000, 1030, 1060, 1090, 1120, 1150, 4150]})
    first = asyncio.run(G.scan_and_remember(pool, owner_id=7))
    assert len(first) == 1 and first[0]["level"] == G.SPIKE
    assert pool.emitted, "новая аномалия должна эмитить событие"
    # повторный проход по тому же ряду — тот же всплеск уже отзвонен
    n_emitted = len(pool.emitted)
    second = asyncio.run(G.scan_and_remember(pool, owner_id=7))
    assert second == [], "тот же всплеск не должен звонить дважды"
    assert len(pool.emitted) == n_emitted


def test_scan_no_owner_is_empty():
    pool = _Pool(channels=[], series_by_cid={})
    assert asyncio.run(G.scan_and_remember(pool, owner_id=0)) == []
