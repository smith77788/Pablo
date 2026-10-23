"""Адаптивный темп действий по времени суток, дню недели и ML-движку.

Выделено из op_worker.py (распил монолита): чистый расчёт задержки между
действиями — ночью паузы усиливаются, в выходные тоже, а поверх накладывается
множитель ML-движка pacing_engine (учёт истории флудов/банов). Состояния уровня
модуля здесь нет; ML-состояние живёт в services.pacing_engine, откуда мы берём
множитель. op_worker импортирует эти имена обратно — контракт
op_worker.get_adaptive_delay / get_adaptive_batch_delay не меняется.
"""
from __future__ import annotations

import datetime as _dt
import logging
import random

from services.pacing_engine import get_pacing_engine

log = logging.getLogger(__name__)

# Множители задержек по часам суток (0-23). Ночью — усиленные паузы, днём — нормальные.
_HOUR_MULTIPLIER = [
    2.0, 2.0, 2.0, 2.0, 2.0, 1.8,
    1.5, 1.2, 1.0, 1.0, 1.0, 1.0,
    1.0, 1.0, 1.0, 1.0, 1.0, 1.1,
    1.2, 1.3, 1.5, 1.8, 2.0, 2.0,
]

_DAY_MULTIPLIER = [1.0, 1.0, 1.0, 1.0, 1.0, 1.2, 1.5]


def get_adaptive_delay(base_delay: float, tz_offset: int = 2, action_type: str = "") -> float:
    """Рассчитать адаптивную задержку с учётом времени суток и дня недели."""
    now = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=tz_offset)
    hour = now.hour
    weekday = now.weekday()  # 0=пн, 6=вс

    hour_mult = _HOUR_MULTIPLIER[hour]
    day_mult = _DAY_MULTIPLIER[weekday]

    ml_mult = get_pacing_engine().get_multiplier(action_type)
    jitter = random.uniform(0.85, 1.15)
    delay = base_delay * hour_mult * day_mult * ml_mult * jitter

    log.debug(
        "adaptive_pacing: base=%.1f h_mult=%.1f d_mult=%.1f ml=%.1f → delay=%.1fs (hour=%d, weekday=%d)",
        base_delay, hour_mult, day_mult, ml_mult, delay, hour, weekday,
    )
    return delay


def get_adaptive_batch_delay(base_delay: float, batch_size: int, tz_offset: int = 2, action_type: str = "") -> float:
    """Адаптивная задержка для батч-операций с учётом размера батча."""
    base = get_adaptive_delay(base_delay, tz_offset, action_type)
    # Большие батчи — увеличиваем задержку пропорционально
    batch_factor = 1.0 + (batch_size / 100) * 0.3  # +30% за каждые 100 аккаунтов
    return base * batch_factor
