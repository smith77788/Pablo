"""Храповик: операции ставятся только через шину, без сырых INSERT в очередь.

Прямой `INSERT INTO operation_queue` обходит ВСЁ, что шина делает по дороге:

  * гейт тарифа (`OP_REGISTRY[op]['min_plan']`) — free-юзер ставил платную
    операцию, если хендлер забыл проверку;
  * предохранитель Ban Weather — операция ставилась даже тогда, когда этот
    паттерн прямо сейчас массово убивает аккаунты владельца;
  * окно идемпотентности — двойной тап кнопки или повтор запроса после таймаута
    порождали ДВЕ операции: двойной расход аккаунтов и удвоенный риск;
  * проверку, что op_type вообще существует в реестре.

Исключение ровно одно — сам `operation_bus`: он и есть та самая запись.

`op_worker` был вторым исключением: рекуррентные операции (автопостинг)
переочередь ставил прямым INSERT. Это означало, что РАСПИСАНИЕ СИЛЬНЕЕ ЗАЩИТЫ —
круг заводился даже тогда, когда предохранитель Ban Weather приостановил этот
тип операций, потому что он массово убивает аккаунты владельца; и что
автопостинг, заведённый на платном плане, работал вечно после отмены подписки.
Переочередь переведена на шину, исключение снято. Не возвращать: если шина
отказывает рекуррентной операции, это ответ, а не препятствие — круг
пропускается, и владельцу уходит уведомление, что расписание остановлено.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"services/operation_bus.py"}


def _sources():
    for base in ("bot", "services"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            yield rel, path.read_text(encoding="utf-8")


def test_no_direct_operation_queue_inserts():
    offenders = []
    for rel, src in _sources():
        for i, line in enumerate(src.split("\n"), 1):
            if re.search(r"INSERT\s+INTO\s+operation_queue", line, re.I):
                offenders.append(f"{rel}:{i}")
    assert not offenders, (
        "операция ставится мимо operation_bus.submit() — обойдены гейт тарифа, "
        "предохранитель Ban Weather и дедуп двойного тапа:\n  "
        + "\n  ".join(offenders)
    )


def test_bus_is_the_only_writer_of_pending_operations():
    """Ни один модуль, кроме шины и воркера, не должен переводить операцию в
    очередь другим путём — например, копированием строки очереди."""
    offenders = []
    for rel, src in _sources():
        for i, line in enumerate(src.split("\n"), 1):
            if re.search(r"INSERT\s+INTO\s+operation_queue", line, re.I) or (
                "operation_queue" in line and re.search(r"\bSELECT\b.*\bINTO\b", line, re.I)
            ):
                offenders.append(f"{rel}:{i}")
    assert not offenders, "\n  ".join(offenders)
