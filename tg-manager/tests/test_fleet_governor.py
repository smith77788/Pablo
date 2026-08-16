"""Глобальный губернатор темпа: давление флота → множитель паузы + кэш.

Проверяем отображение score→множитель, уровни, кэш (не дёргает БД чаще TTL),
инвалидацию и что op_worker масштабирует ТОЛЬКО базовую паузу (не flood).
"""
from __future__ import annotations

import asyncio

from services import fleet_governor as fg


def test_multiplier_steps():
    assert fg.multiplier_for_score(0) == 1.0
    assert fg.multiplier_for_score(24) == 1.0
    assert fg.multiplier_for_score(25) == 1.2
    assert fg.multiplier_for_score(45) == 1.6
    assert fg.multiplier_for_score(70) == 2.5
    assert fg.multiplier_for_score(95) == 4.0


def test_levels():
    assert fg.level_for_multiplier(1.0) == "green"
    assert fg.level_for_multiplier(1.6) == "amber"
    assert fg.level_for_multiplier(2.5) == "red"
    assert fg.level_for_multiplier(4.0) == "red"


class _CountingPressure:
    """Считает вызовы compute_pressure — для проверки кэша."""
    def __init__(self, score):
        self.score = score
        self.calls = 0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_multiplier_cached(monkeypatch):
    fg.invalidate(42)
    counter = _CountingPressure(70)

    async def fake_compute(pool, owner_id):
        counter.calls += 1
        return {"score": counter.score}

    import services.infra_pressure as ip
    monkeypatch.setattr(ip, "compute_pressure", fake_compute)

    m1 = _run(fg.tempo_multiplier(None, 42))
    m2 = _run(fg.tempo_multiplier(None, 42))   # из кэша
    assert m1 == 2.5 and m2 == 2.5
    assert counter.calls == 1                   # второй раз БД не трогали
    fg.invalidate(42)
    _run(fg.tempo_multiplier(None, 42))
    assert counter.calls == 2                    # после сброса — снова посчитали


def test_status_shape(monkeypatch):
    fg.invalidate(7)

    async def fake_compute(pool, owner_id):
        return {"score": 50, "level_label": "Повышенное", "level_emoji": "🟠", "breakdown": {}}

    import services.infra_pressure as ip
    monkeypatch.setattr(ip, "compute_pressure", fake_compute)
    st = _run(fg.status(None, 7))
    assert st["score"] == 50 and st["multiplier"] == 1.6 and st["level"] == "amber"
    assert "explain" in st and st["emoji"] == "🟠"


def test_fail_open_to_one(monkeypatch):
    fg.invalidate(9)

    async def boom(pool, owner_id):
        raise RuntimeError("db down")

    import services.infra_pressure as ip
    monkeypatch.setattr(ip, "compute_pressure", boom)
    assert _run(fg.tempo_multiplier(None, 9)) == 1.0   # не роняем операции


def test_op_worker_scales_base_delay(monkeypatch):
    """op_worker._governed_delay растягивает базовую паузу на множитель губернатора."""
    from services import op_worker

    async def fake_mult(pool, owner_id):
        return 2.5

    # _governor_mult делает `from services import fleet_governor` — патчим модуль.
    monkeypatch.setattr(fg, "tempo_multiplier", fake_mult)
    out = _run(op_worker._governed_delay(None, 1, 10.0))
    assert out == 25.0

