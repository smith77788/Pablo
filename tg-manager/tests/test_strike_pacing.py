"""Адаптивный пейсинг Strike: пауза между волнами растёт с долей флуда.

Раньше межволновые паузы были константами (_WAVE_COOLDOWN) независимо от того,
давит ли Telegram флудом. Теперь множитель паузы зависит от доли аккаунтов,
словивших флуд в предыдущих волнах — только УДЛИНЯЕТ (≥1.0), темп не разгоняем.
"""
from __future__ import annotations

from services import strike_engine as se


def test_flood_ratio_pure():
    assert se._flood_ratio([]) == 0.0
    assert se._flood_ratio([{"peer_reported": True}]) == 0.0
    assert se._flood_ratio([{"_peer_flood": True}, {"peer_reported": True}]) == 0.5
    assert se._flood_ratio([{"error": "FLOOD_WAIT 5"}, {"_peer_flood": True}]) == 1.0


def test_pacing_multiplier_monotonic_and_bounded():
    assert se._pacing_multiplier(0.0) == 1.0            # чисто → без удлинения
    assert se._pacing_multiplier(0.5) == 2.0            # половина флуда → ×2
    # никогда не укорачивает и не превышает кап
    assert se._pacing_multiplier(-1.0) == 1.0
    assert se._pacing_multiplier(1.0) == se._PACING_MAX_MULT
    assert se._pacing_multiplier(5.0) == se._PACING_MAX_MULT
    # монотонность
    vals = [se._pacing_multiplier(x / 10) for x in range(0, 11)]
    assert vals == sorted(vals)


def test_multiplier_never_shortens_base_pause():
    # при любом входе множитель ≥ 1 → пауза не короче базовой
    for fr in (0.0, 0.1, 0.3, 0.7, 1.0):
        assert se._pacing_multiplier(fr) >= 1.0


def test_wave_cooldown_uses_adaptive_multiplier_in_source():
    """Обе межволновые паузы должны домножаться на _pacing_multiplier(_flood_ratio)."""
    import inspect
    src = inspect.getsource(se.staggered_strike)
    # ровно две адаптивные паузы (перед волной 2 и волной 3)
    assert src.count("_pacing_multiplier(_flood_ratio(wave_results))") == 2
    assert "random.uniform(*_WAVE_COOLDOWN) * _mult" in src
