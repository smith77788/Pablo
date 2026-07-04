"""ML Pacing Engine — регрессия: движок должен реально учиться на истории.

Раньше pacing_engine был инертным (get_multiplier читался, но record_result
никто не вызывал → множитель всегда 1.0). Эти тесты фиксируют, что движок
реагирует на флуды/баны/успех, и что он действительно подключён в op_worker.
"""
from __future__ import annotations


def _fresh_engine():
    from services.pacing_engine import PacingEngine

    return PacingEngine()


def test_cold_engine_returns_neutral_multiplier():
    eng = _fresh_engine()
    # < 10 записей — нейтральный множитель, не мешаем разгону
    assert eng.get_multiplier() == 1.0
    assert eng.should_pause() is False


def test_high_flood_rate_slows_down():
    eng = _fresh_engine()
    for _ in range(20):
        eng.record_result(success=False, is_flood=True, action_type="join")
    # >30% флудов → множитель ≥ 2.5 (замедление)
    assert eng.get_multiplier() >= 2.5


def test_bans_trigger_strongest_slowdown():
    eng = _fresh_engine()
    for _ in range(20):
        eng.record_result(success=True)
    for _ in range(3):
        eng.record_result(success=False, is_ban=True, action_type="join")
    # даже небольшой ban_rate → сильное замедление
    assert eng.get_multiplier() >= 1.8


def test_high_success_speeds_up():
    eng = _fresh_engine()
    for _ in range(30):
        eng.record_result(success=True, action_type="post")
    # >90% успеха → ускорение (множитель < 1.0)
    assert eng.get_multiplier() < 1.0


def test_should_pause_on_ban_spike():
    eng = _fresh_engine()
    for _ in range(16):
        eng.record_result(success=True)
    for _ in range(4):
        eng.record_result(success=False, is_ban=True)
    # ban_rate 20% в последних 20 → аварийная пауза
    assert eng.should_pause() is True


def test_stats_track_counts():
    eng = _fresh_engine()
    eng.record_result(success=True)
    eng.record_result(success=False, is_flood=True)
    eng.record_result(success=False, is_ban=True)
    st = eng.get_stats()
    assert st["total"] == 3
    assert st["success"] == 1
    assert st["flood"] == 1
    assert st["ban"] == 1


def test_engine_is_wired_into_op_worker():
    """op_worker должен и читать, и КОРМИТЬ движок (иначе он инертен)."""
    import inspect
    from services import op_worker

    src = inspect.getsource(op_worker)
    assert "get_pacing_engine()" in src, "движок не импортирован в op_worker"
    assert ".record_result(" in src, (
        "op_worker не вызывает record_result — движок останется инертным"
    )
