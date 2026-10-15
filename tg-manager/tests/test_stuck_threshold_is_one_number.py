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

from tests.miniapp_source import api_source

# Имя-метка вместо модуля: источник берём у всего слоя мини-аппа.
_MINI_APP_LAYER = "мини-апп (весь слой)"


def _decider_source(module_name: str, func: str | None) -> str:
    """Исходник места, которое решает «операция зависла»."""
    if module_name == _MINI_APP_LAYER:
        return api_source()
    import importlib
    import inspect as _inspect

    src = _inspect.getsource(importlib.import_module(module_name))
    if not func:
        return src
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == func):
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{func} не найден в {module_name}")


# Места, которые решают «операция зависла». Список держится вручную: новый
# потребитель обязан попасть сюда осознанно, а не унаследовать своё число.
_DECIDERS = [
    ("services.account_monitor", "_recover_stuck_operations"),
    ("services.infra_advisor", None),
    # Мини-апп решает это где-то в своём слое, и ровно где — меняется: карточка
    # «требует внимания» переехала из mini_app_api в mini_app_dashboard при
    # разделении монолита, и привязка к одному модулю сделала набор красным,
    # хотя решение не изменилось. Поэтому спрашиваем весь слой целиком.
    (_MINI_APP_LAYER, None),
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
    src = _decider_source(module_name, func)

    assert "stuck_after_s(" in src, (
        f"{module_name} решает, что операция зависла, своим числом — так и "
        f"разъехались 60 минут, 2 часа и 3 часа при потолке прогона в 6 часов"
    )


@pytest.mark.parametrize("module_name,func", _DECIDERS)
def test_every_decider_skips_operations_that_are_running_now(module_name, func):
    src = _decider_source(module_name, func)

    assert "active_op_ids" in src, (
        f"{module_name} не спрашивает, какие операции выполняются прямо сейчас — "
        f"именно так здоровая многочасовая работа попадала в «зависшие»"
    )


def _flat_rule_offenders(sources) -> list[str]:
    """Строки, где рядом с выборкой 'running' стоит собственный интервал."""
    found: list[str] = []
    for where, src in sources:
        for i, line in enumerate(src.split("\n"), 1):
            if line.strip().startswith("#"):
                continue
            if "status='running'" not in line and "status = 'running'" not in line:
                continue
            if "INTERVAL" in line.upper():
                found.append(f"{where}:{i}: {line.strip()[:90]}")
    return found


def test_the_flat_rule_detector_bites():
    """Зелёный храповик без этой самопроверки ничего не значит."""
    sick = "WHERE status='running' AND started_at < now() - INTERVAL '2 hours'"
    assert _flat_rule_offenders([("демо", sick)])
    healthy = "WHERE status='running' AND id = ANY($1::bigint[])"
    assert not _flat_rule_offenders([("демо", healthy)])


def test_no_decider_keeps_a_flat_time_rule():
    """Прежние плоские пороги не должны вернуться незамеченными.

    Эта проверка была в наборе и пропала при переводе храповика на весь слой
    мини-аппа (решение о зависании уехало из mini_app_api в
    mini_app_dashboard). Возвращаю её в том же стиле: источник мини-аппа —
    целиком слоем, остальные решающие — своими файлами. Без неё порог снова
    можно зашить прямо в запрос, и набор этого не заметит: два parametrize
    выше требуют, чтобы общий порог УПОМИНАЛСЯ, но не запрещают рядом с ним
    собственное число.
    """
    import pathlib as _pl

    root = _pl.Path(__file__).resolve().parents[1]
    sources = [("мини-апп (весь слой)", api_source())]
    for rel in ("services/infra_advisor.py", "services/account_monitor.py"):
        sources.append((rel, (root / rel).read_text(encoding="utf-8")))

    offenders = _flat_rule_offenders(sources)
    assert not offenders, (
        "порог зависания снова зашит рядом с запросом — так и разъехались 60 "
        "минут, 2 часа и 3 часа при потолке прогона в 6 часов:\n  "
        + "\n  ".join(offenders)
    )


def test_the_collector_really_reads_each_decider():
    """Пустой исходник сделал бы оба храповика зелёными навсегда."""
    for module_name, func in _DECIDERS:
        src = _decider_source(module_name, func)
        assert len(src) > 400, f"{module_name}: исходник подозрительно короткий"
