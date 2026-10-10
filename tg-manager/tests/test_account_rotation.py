"""Account Rotation (2C) — регрессия: балансировка нагрузки по last_used.

Раньше get_best_account сортировал кандидатов по last_used ASC, но:
  1. финальный выбор переранжировал топ-10 чисто по риску, игнорируя last_used —
     при ≤10 аккаунтах один и тот же брался снова и снова;
  2. last_used не обновлялся при выборе на этом пути → сортировка не двигалась.
Итог: неравномерный износ, «любимый» аккаунт под повышенным риском бана.

Эти тесты фиксируют, что среди почти равных по риску аккаунтов выбирается
наименее недавно использованный, и что выбор помечает аккаунт использованным.
"""
from __future__ import annotations

import datetime

import pytest

from tests.test_executors import FakePool


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def test_recency_penalty_shape():
    from services.flood_engine import _recency_penalty

    now = 1_000_000.0
    # никогда не использованный → 0 (предпочитаем)
    assert _recency_penalty(None, now) == 0.0
    # только что использованный → максимум
    just = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    assert _recency_penalty(just, now) == pytest.approx(0.08, abs=1e-6)
    # использованный давно (2 часа) → почти 0
    old = datetime.datetime.fromtimestamp(now - 7200, datetime.timezone.utc)
    assert _recency_penalty(old, now) < 0.01


@pytest.mark.asyncio
async def test_prefers_least_recently_used_among_equals():
    from services import flood_engine

    # два равных по trust/риску аккаунта: один только что использован, другой — нет
    rows = [
        {"id": 1, "trust_score": 0.8, "physics_ban_probability": 0.0,
         "last_used": _now(), "session_str": "s1", "proxy_url": None},
        {"id": 2, "trust_score": 0.8, "physics_ban_probability": 0.0,
         "last_used": None, "session_str": "s2", "proxy_url": None},
    ]
    pool = FakePool(fetch=rows)
    best = await flood_engine.get_best_account(pool, owner_id=1, action_type="default")
    assert best is not None and best["id"] == 2  # наименее недавно использованный


@pytest.mark.asyncio
async def test_selection_marks_account_used():
    from services import flood_engine

    rows = [
        {"id": 7, "trust_score": 0.9, "physics_ban_probability": 0.0,
         "last_used": None, "session_str": "s", "proxy_url": None},
    ]
    pool = FakePool(fetch=rows)
    best = await flood_engine.get_best_account(pool, owner_id=1)
    assert best["id"] == 7
    # выбор должен пометить аккаунт использованным (иначе ротация не двигается)
    updates = [q for kind, q in pool.calls if kind == "execute" and "last_used" in q]
    assert updates, "get_best_account не обновил last_used — ротация инертна"


@pytest.mark.asyncio
async def test_trust_still_dominates_over_recency():
    from services import flood_engine

    # аккаунт с заметно большим trust побеждает, даже если недавно использован
    rows = [
        {"id": 1, "trust_score": 0.95, "physics_ban_probability": 0.0,
         "last_used": _now(), "session_str": "s1", "proxy_url": None},
        {"id": 2, "trust_score": 0.50, "physics_ban_probability": 0.0,
         "last_used": None, "session_str": "s2", "proxy_url": None},
    ]
    pool = FakePool(fetch=rows)
    best = await flood_engine.get_best_account(pool, owner_id=1)
    # разница trust 0.45 >> recency-штраф 0.08 → безопасность важнее ротации
    assert best["id"] == 1
