"""Ни один экран не ставит операцию, которую некому исполнить по-настоящему.

ЧТО БЫЛО. Исполнитель `bulk_edit_channels` превращён в отказ: «Массовая смена
названий отключена: выберите один канал» — одно название на весь флот каналов
теперь применяется только с подтверждённым составом
(`services/channel_change_approval`). А экран бота «Массовое редактирование
каналов» продолжал ставить именно эту операцию и отвечал владельцу
«✅ Редактирование каналов запущено, операция #N в очереди». Операция падала
отказом сразу же, и узнать об этом владелец мог только в /ops.

Это тот же класс, что «тихий успех», только наоборот: экран обещает работу,
которой не будет. Цена — владелец считает продукт сломанным и повторяет попытку.

ЧТО ПРОВЕРЯЕМ. Исполнитель, который ОТКАЗЫВАЕТ не начав работу (ни одного
await, единственный return со статусом failed), — это выключенная операция. Ни
один экран бота или мини-аппа не имеет права ставить её через шину: отказ
обязан звучать ДО постановки, словами, на экране.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "services", "op_worker.py")


def _refusing_executors(src: str) -> set[str]:
    """Имена исполнителей, которые отказывают, не начав работу."""
    out = set()
    for node in ast.parse(src).body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not node.name.startswith("_exec_"):
            continue
        if any(isinstance(x, ast.Await) for x in ast.walk(node)):
            continue
        rets = [x for x in ast.walk(node) if isinstance(x, ast.Return)]
        if len(rets) != 1 or not isinstance(rets[0].value, ast.Dict):
            continue
        pairs = {getattr(k, "value", None): getattr(v, "value", None)
                 for k, v in zip(rets[0].value.keys, rets[0].value.values)}
        if pairs.get("status") in ("failed", "error"):
            out.add(node.name)
    return out


def _dispatch(src: str) -> dict[str, str]:
    """Тип операции → имя исполнителя, по таблице исполнителей op_worker."""
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Name) and v.id.startswith("_exec_")):
                out[k.value] = v.id
    return out


def _submitted_types(src: str) -> set[str]:
    """Типы операций, которые этот файл ставит через шину (строкой)."""
    out = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "submit":
            continue
        if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant) \
                and isinstance(node.args[2].value, str):
            out.add(node.args[2].value)
        for kw in node.keywords:
            if kw.arg == "op_type" and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                out.add(kw.value.value)
    return out


def _surfaces():
    for base in ("bot", "services"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, ROOT)
                if rel.replace(os.sep, "/") in ("services/operation_bus.py",
                                                "services/op_worker.py"):
                    continue
                yield rel, path


@pytest.fixture(scope="module")
def worker_src() -> str:
    with open(WORKER, encoding="utf-8") as f:
        return f.read()


def test_the_detectors_see_a_sick_and_a_healthy_sample():
    """Самопроверка измерителей: правило CLAUDE.md, иначе зелёный цвет пустой."""
    sick = (
        "async def _exec_off(pool, bot, op_id, owner_id, params) -> dict:\n"
        "    return {'status': 'failed', 'reason': 'отключено'}\n"
        "async def _exec_real(pool, bot, op_id, owner_id, params) -> dict:\n"
        "    await do_it()\n"
        "    return {'status': 'failed', 'reason': 'не получилось'}\n"
        "TABLE = {'off': _exec_off, 'real': _exec_real}\n"
    )
    assert _refusing_executors(sick) == {"_exec_off"}, "отказ не найден или найден лишний"
    assert _dispatch(sick) == {"off": "_exec_off", "real": "_exec_real"}

    calls = (
        'x = await operation_bus.submit(pool, uid, "off", {}, total_items=1)\n'
        'y = await bus.submit(pool, uid, op_type="real", params={})\n'
        'z = await submit(pool, uid, kind_var, {})\n'
    )
    assert _submitted_types(calls) == {"off", "real"}


def test_no_surface_queues_an_operation_that_only_refuses(worker_src):
    refusing = _refusing_executors(worker_src)
    disabled = {op for op, fn in _dispatch(worker_src).items() if fn in refusing}
    assert disabled, (
        "выключенных операций не нашлось — если их действительно нет, детектор "
        "больше ничего не охраняет и его надо пересмотреть, а не удалять")

    offenders = []
    for rel, path in _surfaces():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for op in sorted(_submitted_types(src) & disabled):
            offenders.append(f"{rel}: ставит «{op}», а исполнитель только отказывает")
    assert not offenders, (
        "экран обещает владельцу работу, которой не будет — отказ обязан "
        "звучать до постановки:\n  " + "\n  ".join(offenders))
