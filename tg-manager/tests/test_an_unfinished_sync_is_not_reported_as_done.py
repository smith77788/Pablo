"""Операция, прерванная по времени, не должна стоять в списке успешной.

Синхронизация контактов флота ограничена двадцатью минутами: зависший на сети
аккаунт не должен держать слот воркера бесконечно. По таймауту исполнитель
возвращал `status='done'` и вдобавок дописывал прогресс до `done_items =
total_items`, то есть операция стояла в списке как успешная и показывала
100%. О срыве владелец узнавал только если открывал текст итога, а всё, что
считает неудачи по статусу (сводки, здоровье флота, разбор «что не доехало»),
срыва не видело вовсе.

У очереди для этого есть отдельный терминальный статус `partial` — «работа
была, но не доведена». Он ничего не перезапускает (во всех дверях стоит рядом
с done/failed/cancelled), поэтому цикла пересинхронизации из этого не выйдет.

Проверяем поведением: исполнитель с зависшей синхронизацией обязан вернуть
неполный статус, не обещать полного прогресса и быть виден счётчиком.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _Pool:
    def __init__(self) -> None:
        self.writes: list[str] = []

    async def execute(self, sql, *args):
        self.writes.append(" ".join(sql.split()))
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None


@pytest.fixture
def hung(monkeypatch):
    """Синхронизация, которая не возвращается: ровно зависший аккаунт."""
    import types

    async def _never(pool, owner_id):
        await asyncio.sleep(3600)

    mod = types.ModuleType("services.contacts_hub.sync_service")
    mod.sync_all_accounts = _never
    pkg = types.ModuleType("services.contacts_hub")
    pkg.sync_service = mod
    monkeypatch.setitem(sys.modules, "services.contacts_hub", pkg)
    monkeypatch.setitem(sys.modules, "services.contacts_hub.sync_service", mod)

    # Ждать двадцать минут тест не должен: таймаут срабатывает сразу.
    _real_wait_for = asyncio.wait_for

    async def _instant(coro, timeout=None):
        task = asyncio.ensure_future(coro)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _instant)
    return mod


def test_a_timed_out_sync_is_reported_as_unfinished(hung):
    from services import op_worker

    before = _total("infragram_op_timeouts_total")
    pool = _Pool()
    res = _run(op_worker._exec_contacts_sync(pool, 501, 9001, {}))

    assert res["status"] == "partial", (
        f"синхронизация сорвалась по таймауту, а в списке операций стоит "
        f"{res['status']!r} — владелец видит успех там, где его не было")
    assert "таймаут" in res["summary"].lower()
    assert _total("infragram_op_timeouts_total") > before, (
        "срыв по времени обязан быть виден счётчиком, а не только в логе")


def test_a_timed_out_sync_does_not_claim_full_progress(hung):
    from services import op_worker

    pool = _Pool()
    _run(op_worker._exec_contacts_sync(pool, 502, 9002, {}))
    bad = [w for w in pool.writes if "done_items=total_items" in w]
    assert not bad, (
        "прогресс дописан до конца у операции, которая до конца не дошла: "
        f"{bad}")


def test_no_executor_returns_success_out_of_an_exception_handler():
    """Храповик: 'done' из обработчика исключения — это успех по ошибке.

    Детектор проверен на здоровом коде: из двадцати шести обработчиков с явным
    статусом двадцать пять возвращают 'failed' или 'requeue', и ровно один
    возвращал 'done'. Находка в единственном числе — признак работающего
    измерителя; дюжина находок означала бы сломанный.
    """
    with open(os.path.join(ROOT, "services/op_worker.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())

    seen, bad = 0, []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef) or not fn.name.startswith("_exec_"):
            continue
        for handler in ast.walk(fn):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for node in ast.walk(handler):
                if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
                    continue
                for k, v in zip(node.value.keys, node.value.values):
                    if (isinstance(k, ast.Constant) and k.value == "status"
                            and isinstance(v, ast.Constant)):
                        seen += 1
                        if v.value == "done":
                            bad.append(f"{fn.name}:{node.lineno}")

    assert seen >= 20, f"найдено статусов в обработчиках: {seen} — разбор сломан"
    assert not bad, (
        "исполнитель отдаёт 'done' из обработчика исключения — операция "
        "закроется как успешная, хотя сорвалась: " + ", ".join(bad) +
        ". Для прерванной работы есть 'partial', для сорванной — 'failed'.")
