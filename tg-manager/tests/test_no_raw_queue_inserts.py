"""Храповик: операции ставятся только через шину, без сырых INSERT в очередь.

Прямой `INSERT INTO operation_queue` обходит ВСЁ, что шина делает по дороге:

  * гейт тарифа (`OP_REGISTRY[op]['min_plan']`) — free-юзер ставил платную
    операцию, если хендлер забыл проверку;
  * предохранитель Ban Weather — операция ставилась даже тогда, когда этот
    паттерн прямо сейчас массово убивает аккаунты владельца;
  * окно идемпотентности — двойной тап кнопки или повтор запроса после таймаута
    порождали ДВЕ операции: двойной расход аккаунтов и удвоенный риск;
  * проверку, что op_type вообще существует в реестре.

Исключения — сам `operation_bus` (он и есть та самая запись) и `op_worker`
(переочередь рекуррентных операций внутри исполнителя).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"services/operation_bus.py", "services/op_worker.py"}


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
