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


def test_per_action_multiplier_isolates_types():
    """get_multiplier(action_type) учится по конкретному типу действия."""
    eng = _fresh_engine()
    # "post" — стабильно успешный
    for _ in range(15):
        eng.record_result(success=True, action_type="post")
    # "strike" — ловит флуды
    for _ in range(15):
        eng.record_result(success=False, is_flood=True, action_type="strike")
    # по типу strike — замедление, по типу post — ускорение
    assert eng.get_multiplier("strike") >= 1.8
    assert eng.get_multiplier("post") < 1.0


def test_per_action_falls_back_to_global_when_sparse():
    """Мало наблюдений по типу → используем глобальный сигнал, не выдумываем."""
    eng = _fresh_engine()
    for _ in range(20):
        eng.record_result(success=False, is_flood=True, action_type="join")
    # по редкому типу "leave" (0 наблюдений) откатываемся к глобальному (флуды)
    assert eng.get_multiplier("leave") >= 2.5
    # глобальный вызов без типа не сломан
    assert eng.get_multiplier() >= 2.5


def test_recommended_delay_reacts_to_fleet_flood():
    """Глобальный ML-темп реально замедляет recommended_delay, а не только админку."""
    import services.pacing_engine as pe
    from services import flood_engine

    saved = pe._engine
    pe._engine = pe.PacingEngine()
    try:
        acc_id = 987654321  # свежий аккаунт без риска
        baseline = flood_engine.recommended_delay(acc_id, "strike")
        # флот массово ловит флуды по strike
        for _ in range(20):
            pe.get_pacing_engine().record_result(
                success=False, is_flood=True, action_type="strike"
            )
        slowed = flood_engine.recommended_delay(acc_id, "strike")
        assert slowed > baseline * 1.5, (
            f"ML-темп не влияет на реальную задержку: {baseline} → {slowed}"
        )
        assert slowed <= 900.0  # cap соблюдён
    finally:
        pe._engine = saved
        flood_engine._flood_state.pop(987654321, None)


def test_recommended_delay_wired_to_pacing_engine():
    """flood_engine должен реально читать движок — иначе он инертен в hot-path."""
    import inspect
    from services import flood_engine

    src = inspect.getsource(flood_engine.recommended_delay)
    assert "get_pacing_engine" in src, (
        "recommended_delay не учитывает ML-темп — движок не влияет на операции"
    )


def test_engine_is_wired_into_op_worker():
    """op_worker должен и читать, и КОРМИТЬ движок (иначе он инертен)."""
    import inspect
    from services import op_worker

    src = inspect.getsource(op_worker)
    assert "get_pacing_engine()" in src, "движок не импортирован в op_worker"
    assert ".record_result(" in src, (
        "op_worker не вызывает record_result — движок останется инертным"
    )
