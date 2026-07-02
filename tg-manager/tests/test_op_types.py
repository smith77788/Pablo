"""Регресс-тест: каждый op_type, который ставит mini app / handlers в очередь,
ДОЛЖЕН диспатчиться воркером. Иначе операция зависает навсегда (класс бага
«pending 29 дней»).

Статически парсим op_type из INSERT ... operation_queue и operation_bus.submit,
и сверяем с диспетчером op_worker.
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _submitted_op_types() -> set[str]:
    types: set[str] = set()
    for rel in ("services/mini_app_api.py",):
        src = _read(rel)
        # INSERT INTO operation_queue(...) VALUES($1,'op_type',...
        for m in re.finditer(r"VALUES\(\$1,\s*'([a-z_]+)'", src):
            types.add(m.group(1))
    # ложные (это не op_type очереди, а kind/asset_type)
    types.discard("note")
    types.discard("post")
    return types


def _dispatched_op_types() -> set[str]:
    src = _read("services/op_worker.py")
    types: set[str] = set()
    for m in re.finditer(r'op_type\s*==\s*"([a-z_]+)"', src):
        types.add(m.group(1))
    for m in re.finditer(r'op_type\s+in\s*\(([^)]*)\)', src):
        for a in re.findall(r'"([a-z_]+)"', m.group(1)):
            types.add(a)
    return types


def test_all_submitted_op_types_are_dispatched():
    submitted = _submitted_op_types()
    dispatched = _dispatched_op_types()
    missing = submitted - dispatched
    assert not missing, (
        f"op_types поставлены mini app, но НЕ диспатчатся воркером "
        f"(зависнут навсегда): {sorted(missing)}"
    )


def test_registry_matches_dispatch():
    """OP_REGISTRY (operation_bus) op_types тоже должны диспатчиться."""
    reg_src = _read("services/operation_bus.py")
    registry = set(re.findall(r'^\s{4}"([a-z_]+)":\s*\{', reg_src, re.M))
    dispatched = _dispatched_op_types()
    missing = registry - dispatched
    assert not missing, f"OP_REGISTRY op_types не диспатчатся: {sorted(missing)}"
