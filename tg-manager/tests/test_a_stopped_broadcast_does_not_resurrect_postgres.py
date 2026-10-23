"""Остановленная рассылка не воскресает после перезапуска процесса.

`broadcaster.cancel()` снимает только asyncio-задачу ЭТОГО процесса.
Терминальный статус в БД пишет сама задача (`broadcaster.run` ловит и
CancelledError ради этой записи), но есть два окна, где она его не напишет:
задача ещё спит в разбеге `start_delay` (он стоит ДО защищённого блока, а у
сетевой рассылки разбег растёт с номером бота) — и задачи вообще нет в
`_running`, потому что процесс перезапустился, а список запущенных рассылок
операция читает из `params`, которые рестарт переживают.

В обоих случаях строка остаётся в ('pending','running') — ровно те статусы,
которые `db.get_interrupted_broadcasts` поднимает на следующем старте процесса.
То есть владелец останавливает рассылку по всей сети ботов, операция честно
закрывается как «остановлена», а после ближайшего деплоя рассылка продолжает
уходить ЖИВЫМ ЛЮДЯМ.

Одиночная рассылка (`_await_broadcast`) метку в БД ставила с самого начала —
четыре ветки сегментов сетевой рассылки звали только `broadcaster.cancel`.
Нужен живой Postgres: проверяется, что строка реально вышла из выборки
воскрешения, а заглушка пула SQL не исполняет.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993424
BOT_ID = 880099771

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID))
    _run(p.execute(
        "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
        "VALUES($1,$2,'stop_test_bot',TRUE,$3)",
        f"1234:stop-test-{BOT_ID}", BOT_ID, OWNER))
    yield p
    _run(p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


def _seed_broadcast(pool, status: str) -> int:
    return int(_run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
        "VALUES($1,'текст рассылки',100,$2,$3) RETURNING id",
        BOT_ID, status, OWNER)))


def _resurrectable(pool, bc_id: int) -> bool:
    """Поднимет ли рассылку выборка воскрешения на следующем старте процесса."""
    from database import db

    rows = _run(db.get_interrupted_broadcasts(pool))
    return any(int(r["id"]) == int(bc_id) for r in rows)


def test_stopping_a_network_broadcast_takes_it_out_of_resurrection(pool):
    """Отмена операции сетевой рассылки закрывает её рассылки и в БД."""
    from services import op_worker

    bc_id = _seed_broadcast(pool, "pending")
    assert _resurrectable(pool, bc_id), (
        "пробник сломан: свежая 'pending' рассылка обязана попадать в выборку "
        "воскрешения")

    op_id = int(_run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
        "                            label, started_at) "
        "VALUES($1,'network_broadcast','cancelled',$2,1,'stop test', now()) RETURNING id",
        OWNER, json.dumps({
            "text": "текст рассылки",
            "segment": "all_each",
            "launched_bot_ids": [BOT_ID],
            "launched_bc_ids": [bc_id],
        }))))
    op_worker._cancel_cache.pop(op_id, None)

    res = _run(op_worker._exec_network_broadcast(
        pool, None, op_id, OWNER,
        {
            "text": "текст рассылки",
            "segment": "all_each",
            "launched_bot_ids": [BOT_ID],
            "launched_bc_ids": [bc_id],
        }))
    assert res["status"] == "cancelled", f"операция не остановилась: {res}"

    assert not _resurrectable(pool, bc_id), (
        "остановленная рассылка осталась в ('pending','running'): ближайший "
        "рестарт процесса поднимет её через resume_interrupted, и сообщение "
        "уйдёт аудитории бота после того, как владелец нажал «стоп»")
    status = _run(pool.fetchval("SELECT status FROM broadcasts WHERE id=$1", bc_id))
    assert status == "cancelled", f"статус рассылки после остановки: {status}"


def test_the_stop_door_keeps_the_counters_and_a_finished_outcome(pool):
    """Дверь не переписывает ни счётчики, ни уже записанный исход.

    Счётчики ведёт сама рассылка, у останавливающего на руках только id. А
    исход, который задача успела записать сама (`partial` после отмены), —
    настоящий, и затирать его «остановлена» значило бы терять число
    отправленных.
    """
    from services import op_worker

    bc_id = _seed_broadcast(pool, "running")
    _run(pool.execute(
        "UPDATE broadcasts SET sent_count=40, failed_count=2 WHERE id=$1", bc_id))
    _run(op_worker._stop_broadcasts(pool, [bc_id]))
    row = _run(pool.fetchrow(
        "SELECT status, sent_count, failed_count, finished_at FROM broadcasts WHERE id=$1",
        bc_id))
    assert row["status"] == "cancelled"
    assert (row["sent_count"], row["failed_count"]) == (40, 2), (
        "дверь обнулила счётчики рассылки — владелец потеряет число отправленных")
    assert row["finished_at"] is not None, "остановленная рассылка осталась без времени конца"

    done = _seed_broadcast(pool, "partial")
    _run(pool.execute("UPDATE broadcasts SET sent_count=7 WHERE id=$1", done))
    _run(op_worker._stop_broadcasts(pool, [done]))
    assert _run(pool.fetchval("SELECT status FROM broadcasts WHERE id=$1", done)) == "partial", (
        "дверь переписала исход, который рассылка уже записала сама")


def test_every_broadcast_stop_goes_through_the_one_door():
    """Храповик: `broadcaster.cancel` зовётся только из двери остановки.

    Снять задачу в процессе — половина остановки; вторая половина (метка в БД)
    нужна ровно там, где задачи в процессе нет. Поэтому прямой вызов
    `broadcaster.cancel` в обход двери — это снова воскресающая рассылка.
    """
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "cancel"
                        and getattr(node.func.value, "id", "") == "broadcaster"):
                    owner.setdefault(node.lineno, fn.name)
    assert owner, "пробник сломан: вызовов broadcaster.cancel в воркере не нашлось"
    stray = {ln: name for ln, name in owner.items() if name != "_stop_broadcasts"}
    assert not stray, (
        "эти остановки рассылки не оставляют метки в БД — рассылка воскреснет "
        f"на следующем рестарте процесса: {stray}")
