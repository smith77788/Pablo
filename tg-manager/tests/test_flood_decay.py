"""Flood-движок — регрессия: штраф после флуда затухает по времени.

record_flood в комментарии обещает «decays over time», но кода не было:
risk_score/consecutive_floods убывали ТОЛЬКО через record_success, который
пишется лишь для части op-типов. Для остальных аккаунт держал завышенную
задержку до рестарта процесса. Тесты фиксируют time-decay штрафа.

monotonic мокается фиксированным значением: в реальном процессе monotonic
велик и last_flood_at (>0) всегда в прошлом; синтетика «now - 3600» без мока
уходила бы в минус на свежем процессе.
"""
from __future__ import annotations

import pytest

_BASE = 1_000_000.0  # фиксированный «monotonic» момент флуда


def _state_flooded_at(monkeypatch, account_id: int, *, age_s: float):
    from services import flood_engine

    flood_engine._flood_state.pop(account_id, None)
    st = flood_engine.get_account_state(account_id)
    st.risk_score = 0.8
    st.consecutive_floods = 4
    st.last_flood_at = _BASE
    monkeypatch.setattr(flood_engine.time, "monotonic", lambda: _BASE + age_s)
    return st


def test_decay_recent_flood_full_penalty(monkeypatch):
    from services.flood_engine import _decayed_flood_penalty

    st = _state_flooded_at(monkeypatch, 9001, age_s=0)
    risk, floods = _decayed_flood_penalty(st)
    assert risk == pytest.approx(0.8)
    assert floods == 4


def test_decay_halves_risk_after_30min(monkeypatch):
    from services.flood_engine import _decayed_flood_penalty

    st = _state_flooded_at(monkeypatch, 9002, age_s=1800)  # 30 минут
    risk, floods = _decayed_flood_penalty(st)
    assert risk == pytest.approx(0.4, abs=0.01)  # 0.8 * 0.5
    assert floods == 2  # −1 за каждые 15 мин → −2


def test_decay_clears_after_an_hour(monkeypatch):
    from services.flood_engine import _decayed_flood_penalty

    st = _state_flooded_at(monkeypatch, 9003, age_s=3600)  # час
    risk, floods = _decayed_flood_penalty(st)
    assert risk == pytest.approx(0.2, abs=0.01)  # 0.8 * 0.25
    assert floods == 0


def test_recommended_delay_drops_as_flood_ages(monkeypatch):
    from services import flood_engine

    _state_flooded_at(monkeypatch, 9004, age_s=0)
    fresh_delay = flood_engine.recommended_delay(9004, "join")

    # тот же аккаунт, флуд состарен на час
    monkeypatch.setattr(flood_engine.time, "monotonic", lambda: _BASE + 3600)
    aged_delay = flood_engine.recommended_delay(9004, "join")
    assert aged_delay < fresh_delay, (
        f"задержка не затухает со временем: {fresh_delay} → {aged_delay}"
    )


def test_no_flood_no_decay_noop():
    from services import flood_engine

    flood_engine._flood_state.pop(9005, None)
    st = flood_engine.get_account_state(9005)  # чистый: last_flood_at=0
    risk, floods = flood_engine._decayed_flood_penalty(st)
    assert risk == 0.0 and floods == 0
