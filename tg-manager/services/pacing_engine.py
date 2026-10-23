from __future__ import annotations
import time
import json
import logging
from collections import deque

log = logging.getLogger(__name__)

class PacingEngine:
    def __init__(self, max_history: int = 500):
        self._history: deque = deque(maxlen=max_history)
        self._ban_count = 0
        self._success_count = 0
        self._flood_count = 0

    def record_result(self, success: bool, is_flood: bool = False, is_ban: bool = False, action_type: str = "") -> None:
        self._history.append({
            "ts": time.time(),
            "ok": success,
            "flood": is_flood,
            "ban": is_ban,
            "type": action_type,
        })
        if is_ban:
            self._ban_count += 1
        elif success:
            self._success_count += 1
        if is_flood:
            self._flood_count += 1

    @staticmethod
    def _multiplier_from(records: list) -> float:
        """Рассчитать множитель задержки по выборке результатов."""
        n = len(records)
        flood_rate = sum(1 for r in records if r["flood"]) / n
        ban_rate = sum(1 for r in records if r["ban"]) / n
        success_rate = sum(1 for r in records if r["ok"]) / n
        if flood_rate > 0.3:
            return 2.5
        elif flood_rate > 0.15:
            return 1.8
        elif ban_rate > 0.05:
            return 3.0
        elif success_rate > 0.9:
            return 0.8
        return 1.0

    def get_multiplier(self, action_type: str = "") -> float:
        """Множитель задержки.

        Если задан action_type и по нему накоплено ≥10 наблюдений — считаем
        множитель по этому типу действия (движок учится, какие именно операции
        сейчас ловят флуды/баны). Иначе — глобальный множитель по всей истории.
        """
        recent = list(self._history)[-50:]
        if action_type:
            typed = [r for r in recent if r["type"] == action_type]
            if len(typed) >= 10:
                return self._multiplier_from(typed)
        if len(self._history) < 10:
            return 1.0
        return self._multiplier_from(recent)

    def should_pause(self) -> bool:
        if len(self._history) < 20:
            return False
        recent = list(self._history)[-20:]
        ban_rate = sum(1 for r in recent if r["ban"]) / len(recent)
        flood_rate = sum(1 for r in recent if r["flood"]) / len(recent)
        return ban_rate > 0.1 or flood_rate > 0.4

    def get_stats(self) -> dict:
        return {
            "total": len(self._history),
            "success": self._success_count,
            "flood": self._flood_count,
            "ban": self._ban_count,
            "success_rate": round(self._success_count / max(len(self._history), 1) * 100, 1),
        }

_engine: PacingEngine | None = None

def get_pacing_engine() -> PacingEngine:
    global _engine
    if _engine is None:
        _engine = PacingEngine()
    return _engine
