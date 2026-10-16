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

Диспетчер спрашиваем НАПРЯМУЮ (`op_worker.dispatch_table()`), а не разбираем
регэкспом по исходнику: пока исполнители жили в цепочке `elif op_type == ...`,
скан был единственным способом и давал ложноотрицательные на форме
`op_type in ("x","y")`. Теперь соответствие — обычный словарь, и сверка точна.
"""
from __future__ import annotations

import inspect

from services import op_worker, operation_bus


def _dispatched_op_types() -> set[str]:
    return set(op_worker.dispatch_table())


def test_every_executor_is_a_coroutine():
    """Исполнитель обязан быть корутиной: воркер зовёт его через await, и
    обычная функция здесь упала бы только в проде, на живой операции."""
    bad = sorted(op for op, fn in op_worker.dispatch_table().items()
                 if not inspect.iscoroutinefunction(fn))
    assert not bad, f"исполнитель должен быть корутиной: {bad}"


def test_unknown_op_type_has_no_executor():
    assert op_worker.handler_for("no_such_op_type_42") is None


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
