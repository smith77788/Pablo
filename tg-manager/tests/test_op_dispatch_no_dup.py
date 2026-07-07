"""Регрессия: в диспетчере op_worker не должно быть дублей веток op_type.

Класс toggleFunnel, но в Python: два `elif op_type == "X"` → вторая ветка мертва
(первая затеняет). Так `bulk_set_profile` матчился дважды (1210 и 1242). Принцип
пользователя: одна стабильная ветка на модуль, без v1/v2-дублей.
"""
from __future__ import annotations

import inspect
import re
from collections import Counter

from services import op_worker


def test_no_duplicate_op_type_dispatch_branches():
    src = inspect.getsource(op_worker)
    # ветки диспетчера — это `elif op_type == "..."` (первая — `if`, обработчики
    # результата используют голый `if ... and`). Собираем ЛИТЕРАЛЫ из elif-веток.
    literals: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("elif op_type =="):
            literals.extend(re.findall(r'op_type == "([a-z0-9_]+)"', s))
    dups = {op: c for op, c in Counter(literals).items() if c > 1}
    assert not dups, (
        f"дубли веток op_type в диспетчере (вторая мертва — затеняется первой): {dups}"
    )
