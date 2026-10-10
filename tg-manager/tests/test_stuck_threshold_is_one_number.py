"""«Операция зависла» решается по одному порогу, а не по пяти разным числам.

ЧТО ЛОМАЛОСЬ. Решали это в пяти местах и каждый по своему числу: 60 минут у
сторожа воркера, 3 часа у сторожа в account_monitor, 2 часа у советчика
infra_advisor, 2 часа у карточки «требует внимания» в мини-аппе. Потолок ОДНОГО
прогона при этом 6 часов (op_worker._OP_TIMEOUT_DEFAULT_S).

Значит все числа, кроме первого, были заведомо меньше разрешённого прогона, и
массовый инвайт с пейсингом на часы — операция, которая обязана идти долго, чтобы
не ловить баны — попадал под них всех, работая ровно как задумано. Сторож в
account_monitor такую операцию обрывал (разбор — в
test_third_watchdog_does_not_kill_live_work), советчик рисовал предупреждение, а
карточка в мини-аппе ещё и подставляла кнопку «поставить очередь на паузу», то
есть прямо подталкивала владельца остановить работающую операцию.

Ложная тревога здесь дороже молчания: владелец отменяет живую работу, а потом
перестаёт верить и настоящим предупреждениям.

ЧТО ТЕПЕРЬ. Порог один на весь продукт — op_worker.stuck_after_s(op_type): свой
потолок прогона этого типа операции плюс запас. Выполняющиеся прямо сейчас
операции исключаются везде, где о зависании СООБЩАЮТ или где его ЛЕЧАТ.
"""
from __future__ import annotations

import ast
import inspect

import pytest

# Места, которые решают «операция зависла». Список держится вручную: новый
# потребитель обязан попасть сюда осознанно, а не унаследовать своё число.
_DECIDERS = [
    ("services.account_monitor", "_recover_stuck_operations"),
    ("services.infra_advisor", None),
    ("services.mini_app_api", None),
]


def test_the_threshold_comes_from_the_run_ceiling():
    from services import op_worker
    from services import operation_bus

    limit = op_worker.stuck_after_s("mass_invite")

    assert limit > operation_bus.timeout_for("mass_invite", op_worker._OP_TIMEOUT_DEFAULT_S), (
        "порог зависания не больше потолка прогона — операция, дошедшая до "
        "потолка, объявляется зависшей ровно в тот момент, когда она ещё вправе "
        "работать"
    )


def test_a_healthy_long_operation_is_inside_the_threshold():
    """Массовый инвайт с пейсингом на часы — не зависание."""
    from services import op_worker

    three_hours = 3 * 3600

    assert op_worker.stuck_after_s("mass_invite") > three_hours, (
        "трёхчасовая операция считается зависшей, хотя потолок одного прогона "
        "вдвое больше"
    )


def test_a_declared_short_type_gets_its_own_threshold(monkeypatch):
    """Порог следует за реестром, а не за одним числом на всех."""
    from services import op_worker
    from services import operation_bus

    monkeypatch.setitem(
        operation_bus.OP_REGISTRY, "contacts_sync",
        dict(operation_bus.OP_REGISTRY["contacts_sync"], timeout_sec=600))

    assert op_worker.stuck_after_s("contacts_sync") < op_worker.stuck_after_s("mass_invite")


@pytest.mark.parametrize("module_name,func", _DECIDERS)
def test_every_decider_uses_the_shared_threshold(module_name, func):
    import importlib

    module = importlib.import_module(module_name)
    src = inspect.getsource(module)

    if func:
        tree = ast.parse(src)
        lines = src.split("\n")
        body = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                body = "\n".join(lines[node.lineno - 1:node.end_lineno])
                break
        assert body is not None, f"{func} не найден в {module_name}"
        src = body

    assert "stuck_after_s(" in src, (
        f"{module_name} решает, что операция зависла, своим числом — так и "
        f"разъехались 60 минут, 2 часа и 3 часа при потолке прогона в 6 часов"
    )


@pytest.mark.parametrize("module_name,func", _DECIDERS)
def test_every_decider_skips_operations_that_are_running_now(module_name, func):
    import importlib

    module = importlib.import_module(module_name)
    src = inspect.getsource(module)

    if func:
        tree = ast.parse(src)
        lines = src.split("\n")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                src = "\n".join(lines[node.lineno - 1:node.end_lineno])
                break

    assert "active_op_ids" in src, (
        f"{module_name} не спрашивает, какие операции выполняются прямо сейчас — "
        f"именно так здоровая многочасовая работа попадала в «зависшие»"
    )


def test_no_decider_keeps_a_flat_two_hour_rule():
    """Прежние плоские пороги не должны вернуться незамеченными."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in ("services/infra_advisor.py", "services/mini_app_api.py",
                "services/account_monitor.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for i, line in enumerate(src.split("\n"), 1):
            if line.strip().startswith("#"):
                continue
            if "status='running'" not in line and "status = 'running'" not in line:
                continue
            if "INTERVAL" in line.upper():
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "порог зависания снова зашит рядом с запросом:\n  " + "\n  ".join(offenders)
    )
