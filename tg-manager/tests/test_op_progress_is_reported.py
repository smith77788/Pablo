"""Массовая операция обязана двигать свою полосу прогресса.

Полоса в мини-аппе (`prog-fill`, «N из M», проценты) и счётчик в панели
операций рисуются ровно по `operation_queue.done_items / total_items`.
Исполнитель, который `done_items` не трогает, оставляет полосу на нуле весь
прогон: владелец видит «0 из 50 · 0 %» полчаса и не может отличить
работающую операцию от зависшей. Это ровно тот случай «массовая операция без
прогресса», который стандарт владельца запрещает наравне с фейковым
прогрессом.

Правило храповика: если операцию хоть где-то ставят с `total_items`, который
не является литералом `1` (то есть единиц работы может быть много), её
исполнитель обязан отчитываться о ходе — сам или через функцию op_worker,
которую он зовёт.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OP_WORKER = os.path.join(ROOT, "services", "op_worker.py")

# Маркеры отчёта о ходе: прямая запись счётчика или общий помощник.
# Отчёт о ходе: запись счётчика внутри цикла. Отметка ПОСЛЕ цикла не считается —
# полоса тогда прыгает с нуля сразу на сто процентов в момент завершения, то есть
# ровно то же «прогресса не видно», только в другой обёртке.
LOOP_MARKERS = ("done_items", "_mark_progress")
# Масштаб, известный только по итогу (замер позиций): полосы во время прогона
# нет и быть не может, но запись должна сойтись с итогом, а не остаться «0 из 0».
SCALE_MARKER = "SET total_items"


@functools.lru_cache(maxsize=1)
def _op_worker() -> tuple[str, list[str], ast.Module]:
    src = open(OP_WORKER, encoding="utf-8").read()
    return src, src.splitlines(), ast.parse(src)


@functools.lru_cache(maxsize=1)
def _functions() -> dict[str, str]:
    """Имя функции модуля op_worker → её исходный текст."""
    _, lines, tree = _op_worker()
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


@functools.lru_cache(maxsize=1)
def _dispatch() -> dict[str, str]:
    """op_type → имя функции-исполнителя, прочитанный из таблицы диспетчера."""
    _, _, tree = _op_worker()
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Name) and v.id.startswith("_exec_")):
                out[k.value] = v.id
    return out


def _sets_scale(src: str) -> bool:
    """Проставляет ли исполнитель масштаб операции (total_items) по итогу."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and SCALE_MARKER in n.value for n in ast.walk(tree))


def _is_mark(node: ast.AST) -> bool:
    """Узел, который реально двигает счётчик прогресса.

    Смотрим именно узлы, а не текст: комментарий, где слово `done_items`
    упомянуто (в том числе объясняющий, зачем отметка нужна), счётчик не
    двигает, а текстовый поиск на нём срабатывает и делает проверку пустой.
    """
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_mark_progress":
            return True
        if isinstance(f, ast.Attribute) and f.attr == "_mark_progress":
            return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        if "done_items" in v and ("UPDATE" in v.upper() or "SET " in v.upper()):
            return True
    return False


def _marks_inside_loop(src: str) -> bool:
    """Есть ли запись счётчика прогресса ВНУТРИ цикла.

    Отметка после цикла не считается: полоса тогда стоит на нуле весь прогон и
    дорисовывается в момент завершения — для владельца это то же самое, что
    прогресса нет.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        if any(_is_mark(inner) for stmt in node.body for inner in ast.walk(stmt)):
            return True
    return False


def _calls(body: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", body))


@functools.lru_cache(maxsize=None)
def _service_source(mod: str) -> str:
    path = os.path.join(ROOT, "services", mod.replace(".", os.sep) + ".py")
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


def _delegated_modules(body: str) -> set[str]:
    """Модули services, которым функция передаёт саму операцию (`op_id=`).

    Движок, получивший op_id, ведёт счётчики операции сам — так устроена
    рассылка в dm_engine. Не учитывать это значит записать честный прогресс
    в нарушители.
    """
    if "op_id=" not in body:
        return set()
    mods: set[str] = set()
    for m in re.finditer(r"from\s+services\s+import\s+([^\n]+)", body):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                mods.add(name)
    for m in re.finditer(r"from\s+services\.([A-Za-z0-9_.]+)\s+import", body):
        mods.add(m.group(1))
    return mods


def _reports_progress(fn_name: str, seen: frozenset[str] = frozenset()) -> bool:
    """Отчитывается ли исполнитель о ходе — сам, через функции op_worker или
    через движок, которому он передал op_id.

    Делегирование учитываем: `_exec_bulk_join` всю работу отдаёт
    `_exec_bulk_join_inner`, `_exec_run_broadcast` — `_await_broadcast`,
    а `_exec_dm_campaign` — `dm_engine.run_campaign`; счётчики двигают они,
    и это честный прогресс.
    """
    if fn_name in seen:
        return False
    funcs = _functions()
    body = funcs.get(fn_name)
    if body is None:
        return False
    if _sets_scale(body) or _marks_inside_loop(body):
        return True
    for mod in _delegated_modules(body):
        src = _service_source(mod)
        if "operation_queue" in src and _marks_inside_loop(src):
            return True
    nxt = seen | {fn_name}
    return any(_reports_progress(c, nxt) for c in _calls(body) if c in funcs and c != fn_name)


def _py_files() -> list[str]:
    out = []
    for sub in ("services", "bot"):
        for dirpath, _, names in os.walk(os.path.join(ROOT, sub)):
            for n in names:
                if n.endswith(".py"):
                    out.append(os.path.join(dirpath, n))
    return out


@functools.lru_cache(maxsize=1)
def _submit_totals() -> dict[str, set[str]]:
    """op_type → множество выражений, которыми задают total_items при постановке."""
    out: dict[str, set[str]] = {}
    for path in _py_files():
        src = open(path, encoding="utf-8").read()
        if "submit(" not in src:
            continue
        for m in re.finditer(r"submit\s*\(", src):
            i, depth = m.end(), 1
            while i < len(src) and depth:
                depth += (src[i] == "(") - (src[i] == ")")
                i += 1
            call = src[m.start():i]
            ти = re.search(r"total_items\s*=\s*([^,)]+)", call)
            for op_type in re.findall(r"[\"']([a-z0-9_]+)[\"']\s*,", call):
                if op_type in _dispatch():
                    out.setdefault(op_type, set()).add(
                        ти.group(1).strip() if ти else "НЕ ЗАДАН")
    return out


def test_dispatch_and_submit_sites_are_found():
    """Антивакуумность: измеритель обязан что-то видеть, иначе проверка пустая."""
    assert len(_dispatch()) >= 50, f"диспетчер прочитан плохо: {len(_dispatch())}"
    assert len(_submit_totals()) >= 20, (
        f"точки постановки операций не найдены: {len(_submit_totals())}")
    # Заведомо честный пример: рассылка отчитывается о ходе через _await_broadcast.
    assert _reports_progress("_exec_run_broadcast"), (
        "измеритель не видит прогресс там, где он точно есть: рассылка ведёт "
        "счётчик в _await_broadcast (делегирование внутри op_worker)")
    assert _reports_progress("_exec_dm_campaign"), (
        "измеритель не видит прогресс там, где он точно есть: DM-кампания ведёт "
        "счётчик в dm_engine.run_campaign (делегирование в движок по op_id)")


def _refuses_without_working(name: str) -> bool:
    """Исполнитель отказывает, не начав работу: это выключенная операция.

    У такой полосы прогресса не бывает по определению — она падает отказом на
    первом же круге. Требовать от неё `done_items` значит получить находку в
    здоровом коде, а детектор с ложной находкой перестаёт защищать (CLAUDE.md).
    Что ни один экран такую операцию не ставит, держит отдельный храповик —
    tests/test_no_surface_queues_a_refusing_operation.py.
    """
    src, _, tree = _op_worker()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name != name:
            continue
        if any(isinstance(x, ast.Await) for x in ast.walk(node)):
            return False
        rets = [x for x in ast.walk(node) if isinstance(x, ast.Return)]
        if len(rets) != 1 or not isinstance(rets[0].value, ast.Dict):
            return False
        pairs = {getattr(k, "value", None): getattr(v, "value", None)
                 for k, v in zip(rets[0].value.keys, rets[0].value.values)}
        return pairs.get("status") in ("failed", "error")
    return False


def test_mass_operations_report_progress():
    """У операции с несколькими единицами работы полоса обязана двигаться."""
    broken: list[str] = []
    for op_type, totals in sorted(_submit_totals().items()):
        # Операция ровно на одну единицу работы полосы не требует: она либо
        # «ещё не начата», либо «готова», промежуточного состояния нет.
        if totals <= {"1"}:
            continue
        fn = _dispatch()[op_type]
        if _refuses_without_working(fn):
            continue
        if not _reports_progress(fn):
            broken.append(f"{op_type} ({fn}): total_items={sorted(totals)}, "
                          f"но done_items никто не пишет")
    assert not broken, (
        "Массовая операция показывает полосу прогресса, которая не двигается — "
        "владелец не отличит работу от зависания:\n  " + "\n  ".join(broken))


def _func_source(path: str, name: str) -> str:
    """Текст функции по границам из ast — не по окну фиксированной длины.

    Окно «от заголовка плюс N символов» молча промахивается, стоит коду
    сдвинуться, и отрицательная проверка внутри него становится правдой
    сама собой (это же стережёт tests/test_no_silently_disabled_guards.py).
    """
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {os.path.basename(path)}")


def test_crosspost_total_matches_what_the_run_covers():
    """«1 из 12» в конце прогона одного правила — это неверный масштаб."""
    body = _func_source(os.path.join(ROOT, "services", "mini_app_api.py"),
                        "crosspost_run")
    assert "operation_bus.submit" in body, "постановка через шину пропала"
    assert re.search(r"1 if [\"']link_id[\"'] in p else min\(", body), (
        "масштаб кросспостинга снова считается не по тому, что реально пройдёт: "
        "прогон одного правила закончится на «1 из 12», а прогон 80 правил — "
        "на «50 из 80» (исполнитель берёт не больше 50 за раз)")
