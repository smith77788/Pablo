import pytest
import time

def test_pacing_engine():
    from services.pacing_engine import PacingEngine
    pe = PacingEngine()
    pe.record_result(True)
    pe.record_result(True)
    pe.record_result(False, is_flood=True)
    assert pe.get_multiplier() >= 0.5
    stats = pe.get_stats()
    assert stats['total'] == 3
    assert stats['success'] == 2

def test_pacing_engine_ban_prediction():
    from services.pacing_engine import PacingEngine
    pe = PacingEngine()
    for _ in range(20):
        pe.record_result(False, is_flood=True)
    assert pe.get_multiplier() > 1.0
    assert pe.should_pause() == True

def test_get_adaptive_delay():
    from services.op_worker import get_adaptive_delay
    d1 = get_adaptive_delay(10.0, tz_offset=2)
    assert isinstance(d1, float)
    assert d1 > 0

def test_get_adaptive_batch_delay():
    from services.op_worker import get_adaptive_batch_delay
    d = get_adaptive_batch_delay(10.0, 100)
    assert d > 10.0

def test_pacing_engine_record_and_get():
    from services.pacing_engine import PacingEngine
    pe = PacingEngine()
    for _ in range(10):
        pe.record_result(True)
    stats = pe.get_stats()
    assert stats['success_rate'] == 100.0
    assert pe.should_pause() == False

def test_pacing_engine_ban_risk():
    from services.pacing_engine import PacingEngine
    pe = PacingEngine()
    for _ in range(15):
        pe.record_result(False, is_ban=True)
    m = pe.get_multiplier()
    assert m > 1.0
