"""«Остановить» слышит каждый исполнитель, у которого есть цикл по целям.

Отмена в продукте немедленная: владелец нажимает «Остановить», и операция
обязана прекратить РЕАЛЬНЫЕ действия на ближайшем чекпоинте, а не доиграть
список до конца. Дверь одна — `op_worker._is_cancelled` (кэш 5 секунд, читает
`op_status.stop_requested`, то есть замечает и удалённую операцию).

Так было у 72 исполнителей из 74. Выпадала выгрузка контактов на устройство
(`_exec_contacts_push`): она идёт по аккаунтам, у каждого выгрузка книги плюс
обратное чтение (до 600 секунд), и на флоте это часы. Владелец остановил
операцию, а она продолжала заливать контакты аккаунт за аккаунтом — заметно
это было только по тому, что книга менялась ПОСЛЕ стопа.

Здесь поведенческая проверка на этот исполнитель и храповик на всех: цикл по
целям без обращения к двери отмены запрещён.
"""
from __future__ import annotations

import ast
import asyncio
import os

import services.op_worker as ow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Дверь отмены во всех её видах: прямая проверка, пауза, слушающая отмену, и
# общая функция решения (её читают исполнители, вынесенные в свои модули).
CANCEL_DOORS = ("_is_cancelled", "_pause_watching_for_cancel", "stop_requested")

# Исполнители, которые спрашивают дверь НЕ сами. Список намеренно короткий: он
# про вынесенные движки, где чекпоинт стоит внутри, а не про «тут неудобно».
DELEGATED = {
    # strike_engine._op_cancelled() читает op_status.stop_requested между
    # волнами — у исполнителя остаётся только разбор параметров.
    "_exec_strike": "services/strike_engine.py",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _executors():
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("_exec_")):
            yield node.name, (ast.get_source_segment(src, node) or ""), node


def test_no_executor_loop_ignores_the_stop_button():
    """Храповик: цикл по целям обязан спрашивать дверь отмены."""
    deaf = []
    seen = 0
    for name, seg, node in _executors():
        seen += 1
        if name in DELEGATED:
            continue
        has_loop = any(isinstance(n, (ast.For, ast.AsyncFor, ast.While))
                       for n in ast.walk(node))
        if has_loop and not any(d in seg for d in CANCEL_DOORS):
            deaf.append(name)
    assert seen > 50, f"пробник не нашёл исполнителей ({seen}) — он сломан"
    assert not deaf, (
        "эти исполнители идут по списку целей, не спрашивая про отмену: "
        f"«Остановить» для них не работает до конца списка — {deaf}")


def test_the_deaf_executor_detector_bites():
    """Самопроверка: на заведомо глухом исполнителе детектор обязан сработать."""
    sick = ("async def _exec_x(pool, op_id):\n"
            "    for t in targets:\n"
            "        await send(t)\n")
    node = ast.parse(sick).body[0]
    seg = ast.get_source_segment(sick, node) or ""
    has_loop = any(isinstance(n, (ast.For, ast.AsyncFor, ast.While))
                   for n in ast.walk(node))
    assert has_loop and not any(d in seg for d in CANCEL_DOORS)
    # А на здоровом — нет.
    healthy = sick.replace("        await send(t)",
                           "        if await _is_cancelled(pool, op_id):\n"
                           "            return {}\n        await send(t)")
    node2 = ast.parse(healthy).body[0]
    seg2 = ast.get_source_segment(healthy, node2) or ""
    assert any(d in seg2 for d in CANCEL_DOORS)


def test_contacts_push_stops_before_touching_the_next_account(monkeypatch):
    """Поведение: после стопа следующий аккаунт книгу не получает."""
    from services.contacts_hub import device_sync

    touched: list[int] = []

    async def _hub(pool, owner_id):
        return [{"phone": "+79990000001", "first_name": "A"}]

    async def _push(session, acc, contacts):
        touched.append(int(acc["id"]))
        return {"sent": 1, "imported": 1, "already": 0, "left": 0}

    async def _acc(pool, acc_id, owner_id):
        return {"id": acc_id, "phone": f"+7999000{acc_id}", "session_str": "s"}

    monkeypatch.setattr(device_sync, "hub_contacts", _hub)
    monkeypatch.setattr(device_sync, "push_contacts", _push)
    from database import db as _dbmod
    monkeypatch.setattr(_dbmod, "get_account_for_telethon", _acc)

    async def _no_quarantine(pool, acc_id):
        return False
    monkeypatch.setattr(ow._infra_mem, "is_account_quarantined", _no_quarantine)

    async def _claim(acc_id):
        return True
    monkeypatch.setattr(ow, "try_claim_account", _claim)

    async def _release(ids):
        return None
    monkeypatch.setattr(ow, "release_accounts", _release)

    async def _noop_execute(*a, **k):
        return "UPDATE 1"
    monkeypatch.setattr(ow, "_safe_execute", _noop_execute)

    async def _sync(pool, owner_id, acc_id):
        return None
    from services.contacts_hub import sync_service
    monkeypatch.setattr(sync_service, "sync_account", _sync)

    # Отмена приходит ПОСЛЕ первого аккаунта — ровно как нажатие кнопки в
    # середине выгрузки по флоту.
    calls = {"n": 0}

    async def _cancelled(pool, op_id):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(ow, "_is_cancelled", _cancelled)

    res = _run(ow._exec_contacts_push(
        None, 777, 42, {"account_ids": [11, 22, 33]}))

    assert touched == [11], (
        "после «Остановить» выгрузка продолжилась на следующие аккаунты: "
        f"книгу получили {touched}")
    assert res["status"] == "cancelled", (
        f"операция отчиталась статусом {res['status']!r} вместо отмены")
    assert res["ok"] == 1, f"потеряна уже сделанная работа: {res}"
