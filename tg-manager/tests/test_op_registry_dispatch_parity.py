"""Регресс: паритет реестра операций (operation_bus.OP_REGISTRY) и диспетчера
исполнителей (op_worker).

Класс бага (найден ручной трассировкой цепочки, формальные правила не поймали):
  * op_type ЗАРЕГИСТРИРОВАН, но НЕ диспетчеризуется → операция встанет pending
    навсегда (нет исполнителя);
  * op_type ДИСПЕТЧЕРИЗУЕТСЯ, но НЕ в реестре → (а) `operation_bus.submit` падает
    ValueError по контракту модуля; (б) list_active/get_status показывают в панели
    операций сырой технический op_type и generic ⚙️ вместо человекочитаемого
    лейбла/иконки.

Реестр приведён в полный паритет с диспетчером op_worker; тест это фиксирует,
чтобы расхождение не вернулось при добавлении новых op_type.

Диспетчер разбирается статически (без импорта op_worker — у него тяжёлые
Telethon-зависимости), учитывая ОБЕ формы сравнения: `op_type == "x"` и
`op_type in ("x","y")` (вторая форма — та самая, что даёт ложноотрицательные при
наивном скане).
"""
from __future__ import annotations

import os
import re

from services import operation_bus

_WORKER = os.path.join(os.path.dirname(__file__), "..", "services", "op_worker.py")


def _dispatched_op_types() -> set[str]:
    src = open(_WORKER, encoding="utf-8").read()
    disp = set(re.findall(r'op_type\s*==\s*"([a-z_]+)"', src))
    for grp in re.findall(r"op_type\s+in\s+\(([^)]*)\)", src):
        disp |= set(re.findall(r'"([a-z_]+)"', grp))
    return disp


def test_every_registered_op_is_dispatched():
    reg = set(operation_bus.OP_REGISTRY.keys())
    disp = _dispatched_op_types()
    missing = reg - disp
    assert not missing, (
        f"op_type в OP_REGISTRY без исполнителя в op_worker (встанут pending): {sorted(missing)}"
    )


def test_every_dispatched_op_is_registered():
    reg = set(operation_bus.OP_REGISTRY.keys())
    disp = _dispatched_op_types()
    unregistered = disp - reg
    assert not unregistered, (
        "op_type с исполнителем, но без записи в OP_REGISTRY "
        f"(submit→ValueError, сырой лейбл в панели операций): {sorted(unregistered)}"
    )


def test_registry_entries_have_required_metadata():
    """Каждая запись реестра — с непустым description и icon (иначе панель
    операций деградирует в сырой op_type/generic ⚙️, ради чего паритет и вводился)."""
    bad = []
    for op_type, meta in operation_bus.OP_REGISTRY.items():
        if not (meta.get("description") or "").strip() or not (meta.get("icon") or "").strip():
            bad.append(op_type)
    assert not bad, f"записи реестра без description/icon: {sorted(bad)}"
