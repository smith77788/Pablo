from __future__ import annotations
import time
import json
import logging
from collections import deque

log = logging.getLogger(__name__)

# Сколько последних исходов по ФЛОТУ держит общее окно. Решения движка
# смотрят не дальше 50 (get_multiplier) — окно берём с запасом.
SHARED_WINDOW = 100


class PacingEngine:
    """Темп флота по последним исходам операций.

    ОБЩЕЕ ОКНО. Раньше история жила только в памяти процесса: рестарт (то есть
    каждый деплой) обнулял накопленную осторожность, а второй исполнитель
    считал свой темп по своим исходам — флот разгонялся вдвое при флудах,
    которые видел сосед. Теперь исходы уходят в Postgres
    (`fleet_pacing_events`), и окно решений собирается из общих строк всех
    процессов (`services/pacing_store.py`). Пока окно из базы не пришло ни
    разу (нет базы, первые секунды после старта, тесты) — работаем по своей
    истории, как раньше.
    """

    def __init__(self, max_history: int = 500):
        self._history: deque = deque(maxlen=max_history)
        self._ban_count = 0
        self._success_count = 0
        self._flood_count = 0
        # Исходы, ещё не записанные в базу (их забирает pacing_store).
        self._pending: list[dict] = []
        # Последнее окно из базы — исходы ВСЕХ процессов, старые первыми.
        # None — окна ещё не было, решаем по своей истории.
        self._shared: "list[dict] | None" = None

    def record_result(self, success: bool, is_flood: bool = False, is_ban: bool = False, action_type: str = "") -> None:
        rec = {
            "ts": time.time(),
            "ok": success,
            "flood": is_flood,
            "ban": is_ban,
            "type": action_type,
        }
        self._history.append(rec)
        self._pending.append(rec)
        # Без базы очередь не разбирает никто — не даём ей расти без предела.
        if len(self._pending) > SHARED_WINDOW * 10:
            del self._pending[: len(self._pending) - SHARED_WINDOW * 10]
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

    def drain_pending(self) -> list[dict]:
        """Забрать исходы для записи в базу. Список подменяется целиком, до
        любого await у вызывающего: исход, пришедший во время записи, ляжет в
        новую очередь, а не потеряется."""
        out, self._pending = self._pending, []
        return out

    def requeue(self, records: list[dict]) -> None:
        """Вернуть исходы, которые записать не удалось, в начало очереди."""
        if records:
            self._pending = list(records) + self._pending

    def set_shared(self, records: list[dict]) -> None:
        """Принять окно из базы (старые первыми)."""
        self._shared = list(records)[-SHARED_WINDOW:]

    def _window(self, n: int) -> list:
        """Последние n исходов, по которым принимается решение.

        Общее окно из базы плюс свои исходы, которые туда ещё не записаны:
        иначе флуд, пойманный секунду назад, ждал бы цикла записи, прежде чем
        замедлить темп."""
        if self._shared is None:
            return list(self._history)[-n:]
        return (self._shared + self._pending)[-n:]

    def get_multiplier(self, action_type: str = "") -> float:
        """Множитель задержки.

        Если задан action_type и по нему накоплено ≥10 наблюдений — считаем
        множитель по этому типу действия (движок учится, какие именно операции
        сейчас ловят флуды/баны). Иначе — глобальный множитель по всей истории.
        """
        recent = self._window(50)
        if action_type:
            typed = [r for r in recent if r["type"] == action_type]
            if len(typed) >= 10:
                return self._multiplier_from(typed)
        if len(recent) < 10:
            return 1.0
        return self._multiplier_from(recent)

    def should_pause(self) -> bool:
        recent = self._window(20)
        if len(recent) < 20:
            return False
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
