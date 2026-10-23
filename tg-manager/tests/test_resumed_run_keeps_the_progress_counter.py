"""Возобновлённый прогон не теряет прогресс на уже сделанных целях.

Полтора десятка исполнителей читают журнал целей (`completed_targets`,
`completed_steps`, `settled_targets`, `completed_account_targets`) и пропускают
то, что закрыл прошлый прогон. Пропуск обязан двигать `done_items` — счётчик
обнуляется на каждом возврате в очередь (повтор, деплой, потолок времени), а
`total_items` остаётся от первого замаха. Прогон, который молча проскочил 900
закрытых целей и сделал последние 100, заканчивается с `done_items=100` при
`total_items=1000`, и операция, доведённая до конца, объявляется частичной
(ровно та ложь, из-за которой появился статус 'partial': «203 из 380»).
Владельцу при этом предлагают повторить всю работу заново, а в прогрессе он всё
это время видит ноль.

Это ратчет: он не ищет поломку в конкретном исполнителе, а запрещает появление
нового пропуска без счётчика. Измеритель проверяет себя на здоровом коде — если
он перестал видеть исполнителей с журналом, тест падает, а не молчит.
"""
from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"

_DOORS = {
    "completed_targets",
    "completed_steps",
    "settled_targets",
    "completed_account_targets",
}

# Сколько строк до `continue` считаем «телом пропуска»: там и стоит инкремент.
_WINDOW = 12


def _door_sets(fn: ast.AST) -> set[str]:
    """Имена переменных, в которые этот исполнитель сложил журнал."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        callee = value.func
        callee_name = (
            callee.attr if isinstance(callee, ast.Attribute)
            else callee.id if isinstance(callee, ast.Name) else "")
        if callee_name not in _DOORS:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                names.add(tgt.id)
    return names


def _executors_with_a_journal():
    tree = ast.parse(_SRC.read_text())
    lines = _SRC.read_text().splitlines()
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not fn.name.startswith("_exec_"):
            continue
        sets = _door_sets(fn)
        if not sets:
            continue
        body = lines[fn.lineno - 1:fn.end_lineno]
        # Исполнитель, который прогресс вообще не ведёт, под это правило не
        # попадает: ему нечего терять, и требовать от него счётчик — выдумка.
        if "done_items" not in "\n".join(body):
            continue
        out.append((fn.name, fn.lineno, sets, body))
    return out


def test_the_detector_sees_the_executors_with_a_journal():
    """Измеритель на здоровом коде: журнал читают многие, а не ноль."""
    found = _executors_with_a_journal()
    names = sorted(n for n, *_ in found)
    assert len(found) >= 10, f"найдено исполнителей с журналом: {names} — разбор сломан"
    assert "_exec_gp_bulk_apply" in names, (
        "пакетное применение читает журнал — измеритель обязан его видеть")


def test_every_skip_of_a_done_target_moves_the_progress_counter():
    """Пропуск закрытой цели двигает done_items — иначе итог будет 'partial'."""
    offenders: list[str] = []
    checked = 0
    for name, lineno, sets, body in _executors_with_a_journal():
        for idx, raw in enumerate(body):
            if raw.strip() != "continue":
                continue
            window = body[max(0, idx - _WINDOW):idx]
            text = "\n".join(window)
            # Пропуск именно по журналу: в условии стоит проверка вхождения в
            # набор закрытых целей, а не любой другой `continue` в цикле.
            if not any(f"in {s}" in text for s in sets):
                continue
            checked += 1
            if "done_items" not in text:
                offenders.append(f"{name} (строка {lineno + idx})")
    assert checked >= 10, (
        f"проверено пропусков: {checked} — измеритель перестал их находить, "
        "правило больше ничего не охраняет")
    assert not offenders, (
        "пропуск закрытой цели не двигает done_items: "
        + ", ".join(offenders)
        + ". Возобновлённый прогон закончится с недобором по прогрессу, и "
          "доведённая до конца операция будет объявлена частичной.")
