"""Ghost под губернатором и локальной ночью: фоновый шум гасим при рисках."""
from __future__ import annotations

import asyncio
import os

from services import ghost_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_skip_probability_scales_with_pressure():
    assert ghost_engine._ghost_skip_probability("green", False) == 0.0
    assert ghost_engine._ghost_skip_probability("amber", False) == 0.4
    assert ghost_engine._ghost_skip_probability("red", False) == 0.8


def test_local_night_raises_skip_even_when_calm():
    assert ghost_engine._ghost_skip_probability("green", True) == 0.7
    # ночь + red → берём максимум (0.8), не суммируем
    assert ghost_engine._ghost_skip_probability("red", True) == 0.8


def test_allowed_fail_open_without_owner():
    # owner_id=None → губернатор не спрашивается, гео нет → всегда разрешено
    async def go():
        return await ghost_engine._ghost_allowed(None, None, None, None)
    assert asyncio.run(go()) is True


def test_process_profile_calls_gate_before_connect():
    src = open(os.path.join(ROOT, "services", "ghost_engine.py"), encoding="utf-8").read()
    gate = src.index("await _ghost_allowed(")
    connect = src.index("_make_client(session")
    # skip-гейт срабатывает ДО создания клиента (не плодит подключений)
    assert gate < connect
    assert "p.geo_country" in src  # гео тянется из прокси
