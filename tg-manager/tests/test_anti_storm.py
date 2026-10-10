"""Anti-Storm — событийный предохранитель против ВОЛНЫ банов флота.

Губернатор тормозит по инерционному давлению; Anti-Storm ловит внезапную чистку
в момент банов и резко клэмпит темп на фиксированное окно. Проверяем сердце —
чистый классификатор и логику активного множителя (что и защищает флот), плюс
что арминг реально пишет состояние, будит губернатор и не понижает уже
объявленный шторм.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from services import anti_storm as A

T0 = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


# ── Чистый классификатор ─────────────────────────────────────────────────────

def test_calm_below_watch_threshold():
    d = A.classify(2, fleet_size=50)
    assert d["level"] == A.CALM and d["mult"] == 1.0


def test_watch_on_absolute_threshold():
    d = A.classify(A.WATCH_ABS, fleet_size=50)
    assert d["level"] == A.WATCH and d["mult"] == A.WATCH_MULT


def test_storm_on_absolute_threshold():
    d = A.classify(A.STORM_ABS, fleet_size=100)
    assert d["level"] == A.STORM and d["mult"] == A.DEEP_SLEEP_MULT


def test_storm_by_share_on_large_fleet():
    # 2 из 15 = 13% ≥ 10% при флоте ≥ MIN_FLEET_FOR_SHARE, но 2 < WATCH_ABS(3)?
    # берём hit=3 (watch по абсолюту) при флоте, где доля тоже даёт шторм.
    d = A.classify(3, fleet_size=20)   # 3/20 = 15% ≥ 10%
    assert d["level"] == A.STORM


def test_small_fleet_ignores_share():
    """На маленьком флоте доля НЕ считается: 2 из 3 — не шторм, это штиль/watch
    по абсолюту, а не «66% флота выкосило»."""
    d = A.classify(2, fleet_size=3)    # 66% но флот < MIN_FLEET_FOR_SHARE
    assert d["level"] == A.CALM        # 2 < WATCH_ABS → штиль, не шторм


def test_zero_and_negative_are_calm():
    assert A.classify(0, 0)["level"] == A.CALM
    assert A.classify(-5, -1)["level"] == A.CALM


# ── Активный множитель из состояния ──────────────────────────────────────────

def test_no_state_is_neutral():
    assert A.active_multiplier(None) == 1.0
    assert A.active_multiplier({}) == 1.0


def test_active_state_returns_multiplier():
    st = A.build_state({"level": A.STORM, "mult": A.DEEP_SLEEP_MULT}, now=T0)
    assert A.active_multiplier(st, now=T0 + timedelta(minutes=5)) == A.DEEP_SLEEP_MULT


def test_expired_state_decays_to_neutral():
    """Забытый флаг не морозит флот: срок истекает сам."""
    st = A.build_state({"level": A.STORM, "mult": A.DEEP_SLEEP_MULT}, now=T0)
    assert A.active_multiplier(st, now=T0 + timedelta(minutes=A.HOLD_MIN + 1)) == 1.0


def test_active_multiplier_parses_iso_until():
    """until в organism_state лежит строкой — множитель должен её понимать."""
    st = {"level": A.STORM, "mult": 12.0,
          "until": (T0 + timedelta(minutes=10)).isoformat()}
    assert A.active_multiplier(st, now=T0) == 12.0


# ── build_state ──────────────────────────────────────────────────────────────

def test_build_state_calm_is_none():
    assert A.build_state({"level": A.CALM, "mult": 1.0}) is None


def test_build_state_sets_hold_window():
    st = A.build_state({"level": A.STORM, "mult": A.DEEP_SLEEP_MULT}, now=T0)
    until = datetime.fromisoformat(st["until"])
    assert until == T0 + timedelta(minutes=A.HOLD_MIN)
    assert st["level"] == A.STORM


# ── Тонкие обёртки БД: арминг end-to-end на заглушке пула ────────────────────

class _StormPool:
    """Заглушка: раздаёт fetchval по тексту запроса, держит organism_state в
    памяти (заглушка типы связывания не ловит — CLAUDE.md; здесь важна логика)."""

    def __init__(self, distinct_hit: int, fleet: int, state=None):
        self._hit = distinct_hit
        self._fleet = fleet
        self._state = state          # уже распарсенный dict или None
        self.emitted = []
        self.state_writes = []

    async def fetchval(self, q, *a):
        if "COUNT(DISTINCT account_id)" in q:
            return self._hit
        if "FROM tg_accounts" in q:
            return self._fleet
        return 0

    async def fetchrow(self, q, *a):
        if "organism_state" in q:
            if self._state is None:
                return None
            return {"value": self._state}
        return None

    async def execute(self, q, *a):
        if "organism_state" in q:
            # a = (owner_id, key, json_value)
            self._state = json.loads(a[2])
            self.state_writes.append(self._state)
        elif "organism_events" in q:
            self.emitted.append(a)
        return "OK"


def test_check_and_arm_storm_writes_state():
    pool = _StormPool(distinct_hit=A.STORM_ABS, fleet=50)
    d = asyncio.run(A.check_and_arm(pool, owner_id=7))
    assert d["level"] == A.STORM
    assert pool.state_writes, "шторм должен армировать organism_state"
    assert pool.state_writes[-1]["level"] == A.STORM


def test_check_and_arm_calm_does_not_arm():
    pool = _StormPool(distinct_hit=1, fleet=50)
    d = asyncio.run(A.check_and_arm(pool, owner_id=7))
    assert d["level"] == A.CALM
    assert not pool.state_writes, "штиль ничего не армирует"


def test_check_and_arm_no_owner_is_calm():
    pool = _StormPool(distinct_hit=99, fleet=50)
    d = asyncio.run(A.check_and_arm(pool, owner_id=0))
    assert d["level"] == A.CALM
    assert not pool.state_writes


def test_current_reflects_active_storm():
    st = A.build_state({"level": A.STORM, "mult": A.DEEP_SLEEP_MULT})
    pool = _StormPool(distinct_hit=0, fleet=50, state=st)
    cur = asyncio.run(A.current(pool, owner_id=7))
    assert cur["level"] == A.STORM and cur["mult"] == A.DEEP_SLEEP_MULT


def test_current_calm_when_no_state():
    pool = _StormPool(distinct_hit=0, fleet=50, state=None)
    cur = asyncio.run(A.current(pool, owner_id=7))
    assert cur["level"] == A.CALM and cur["mult"] == 1.0


def test_storm_notifies_owner_only_once():
    """Владельцу «волна банов» приходит на ЭСКАЛАЦИИ, а не на каждый бан подряд."""
    sent = []

    class _Bot:
        async def send_message(self, *a, **k):
            sent.append(a)

    bot = _Bot()
    pool = _StormPool(distinct_hit=A.STORM_ABS, fleet=50)
    asyncio.run(A.check_and_arm(pool, owner_id=7, bot=bot))
    assert len(sent) == 1, "первый шторм — одно уведомление"
    # следующий бан в уже активном шторме не должен слать второе
    asyncio.run(A.check_and_arm(pool, owner_id=7, bot=bot))
    assert len(sent) == 1, "повторный бан в активном шторме не спамит владельца"
