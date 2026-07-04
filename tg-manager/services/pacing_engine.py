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

    def get_multiplier(self, action_type: str = "") -> float:
        if len(self._history) < 10:
            return 1.0
        recent = list(self._history)[-50:]
        flood_rate = sum(1 for r in recent if r["flood"]) / len(recent)
        ban_rate = sum(1 for r in recent if r["ban"]) / len(recent)
        success_rate = sum(1 for r in recent if r["ok"]) / len(recent)
        if flood_rate > 0.3:
            return 2.5
        elif flood_rate > 0.15:
            return 1.8
        elif ban_rate > 0.05:
            return 3.0
        elif success_rate > 0.9:
            return 0.8
        return 1.0

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
