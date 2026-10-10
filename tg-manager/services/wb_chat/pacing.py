"""Пейсинг WB-операций: адаптивная задержка + circuit breaker.

Самодостаточный аналог services/flood_engine.py, но без завязки на типы ошибок
Telegram. Идея та же: базовая задержка на тип действия, рост при обнаружении
ограничений (flood) и плавное снижение при стабильной работе; предохранитель
останавливает поток при серии сбоев, чтобы не сжечь аккаунты.
"""

from __future__ import annotations

import random

# Консервативные базовые задержки между действиями (секунды) на тип.
_BASELINE = {
    "dm": 8.0,
    "join": 20.0,
    "default": 5.0,
}


def base_delay(action_type: str) -> float:
    return _BASELINE.get(action_type, _BASELINE["default"])


def jittered(delay: float, *, spread: float = 0.35) -> float:
    """Добавить случайный разброс (±spread) — против машинной регулярности."""
    if delay <= 0:
        return 0.0
    return max(0.0, delay * (1.0 + random.uniform(-spread, spread)))


class Pacer:
    """Состояние пейсинга одного потока операции.

    delay растёт мультипликативно на flood и медленно спадает на успехе.
    Circuit breaker размыкается после `breaker_threshold` подряд идущих сбоев."""

    def __init__(
        self,
        action_type: str = "default",
        *,
        breaker_threshold: int = 5,
        max_delay: float = 300.0,
    ) -> None:
        self.action_type = action_type
        self._delay = base_delay(action_type)
        self._base = base_delay(action_type)
        self._max = max_delay
        self._fails = 0
        self._breaker_threshold = breaker_threshold

    @property
    def delay(self) -> float:
        return self._delay

    @property
    def tripped(self) -> bool:
        """Разомкнут ли предохранитель (пора остановить поток)."""
        return self._fails >= self._breaker_threshold

    def next_delay(self) -> float:
        """Задержка перед следующим действием (с разбросом)."""
        return jittered(self._delay)

    def on_success(self) -> None:
        self._fails = 0
        # Плавно возвращаемся к базовой задержке.
        self._delay = max(self._base, self._delay * 0.9)

    def on_flood(self, wait_seconds: float) -> None:
        """Ограничение от WB: подчиняемся указанному ожиданию и повышаем базу."""
        self._fails += 1
        self._delay = min(self._max, max(self._delay * 1.5, wait_seconds))

    def on_error(self) -> None:
        self._fails += 1
        self._delay = min(self._max, self._delay * 1.2)
