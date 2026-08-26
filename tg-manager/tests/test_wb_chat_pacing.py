"""Пейсинг WB-операций: адаптация задержки и circuit breaker (без БД/сети)."""
from __future__ import annotations

from services.wb_chat.pacing import Pacer, base_delay, jittered


def test_base_delay_per_action():
    assert base_delay("dm") == 8.0
    assert base_delay("join") == 20.0
    assert base_delay("unknown") == base_delay("default")


def test_jitter_within_bounds():
    for _ in range(100):
        v = jittered(10.0, spread=0.3)
        assert 7.0 <= v <= 13.0
    assert jittered(0.0) == 0.0


def test_flood_increases_delay_and_counts_failure():
    p = Pacer("dm")
    start = p.delay
    p.on_flood(30)
    assert p.delay >= 30            # подчиняемся указанному ожиданию
    assert p.delay > start


def test_success_recovers_delay_and_resets_failures():
    p = Pacer("dm")
    p.on_flood(30)
    p.on_success()
    assert p.tripped is False
    # После успеха задержка ползёт обратно к базовой (не мгновенно).
    assert p.delay <= 30


def test_circuit_breaker_trips_after_threshold():
    p = Pacer("dm", breaker_threshold=3)
    assert p.tripped is False
    p.on_error(); p.on_error()
    assert p.tripped is False
    p.on_error()
    assert p.tripped is True


def test_success_clears_breaker():
    p = Pacer("dm", breaker_threshold=2)
    p.on_error(); p.on_error()
    assert p.tripped is True
    p.on_success()
    assert p.tripped is False


def test_delay_capped_at_max():
    p = Pacer("dm", max_delay=50.0)
    for _ in range(20):
        p.on_flood(999)
    assert p.delay <= 50.0
