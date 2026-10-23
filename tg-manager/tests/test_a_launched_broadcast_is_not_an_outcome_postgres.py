"""Сетевая рассылка: «задачи созданы» — не исход операции.

Одиночная рассылка сопровождается до конца (`_await_broadcast`), и в её
докстринге написано, почему. Сетевая — не сопровождалась: исполнитель заводил
задачи по всем ботам, писал `done_items = total_items = число запущенных ботов`
и возвращал `done`. Операция закрывалась словами «📢 Сетевая рассылка запущена,
получателей: 40 000» в ту же секунду, когда не ушло ещё ни одного сообщения —
бот с номером N в этот момент даже не вышел из разбега (`order * 2s`).

Цена — та же тройка, что уже была описана для одиночной:
  • счётчик врал: «100%» означало «задачи созданы», и `_progress_monitor`
    рассылал по нему бодрые отчёты;
  • остановить сетевую рассылку было нельзя вовсе — отмена операции достаёт
    рассылки только пока идёт цикл запуска (секунды), а дальше операция уже
    терминальна, и сообщения уходят по ВСЕЙ аудитории сети ботов до конца;
  • оборвавшийся залп числился успешным: операция в `done`, поэтому ни сторож
    зависших, ни повтор её не касались.

Нужен живой Postgres: исполнитель читает ботов, аудиторию и строки рассылок
настоящими запросами.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993425
BOT_ID = 880099772

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
    _run(p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID))
    _run(p.execute(
        "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
        "VALUES($1,$2,'outcome_test_bot',TRUE,$3)",
        f"1234:outcome-test-{BOT_ID}", BOT_ID, OWNER))
    for uid in (7001, 7002, 7003):
        _run(p.execute(
            "INSERT INTO bot_users(bot_id, user_id, is_active) VALUES($1,$2,TRUE)",
            BOT_ID, uid))
    yield p
    _run(p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID))
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


def _seed_broadcast(pool, status: str, sent: int, failed: int) -> int:
    return int(_run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, "
        "                       sent_count, failed_count) "
        "VALUES($1,'текст залпа',3,$2,$3,$4,$5) RETURNING id",
        BOT_ID, status, OWNER, sent, failed)))


def _exec(pool, bc_id: int) -> dict:
    """Прогнать исполнителя так, чтобы он ничего не отправлял наружу.

    Бот объявлен уже запущенным прошлым прогоном (`launched_bot_ids`), поэтому
    `_nb_launch` идёт веткой «уже заведено» и не создаёт ни рассылки, ни задачи:
    остаётся ровно то, что проверяется — чем операция считает исход.
    """
    from services import op_worker

    params = {
        "text": "текст залпа",
        "segment": "all_each",
        "launched_bot_ids": [BOT_ID],
        "launched_bc_ids": [bc_id],
    }
    op_id = int(_run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
        "                            label, started_at) "
        "VALUES($1,'network_broadcast','running',$2,1,'outcome test', now()) RETURNING id",
        OWNER, json.dumps(params))))
    op_worker._cancel_cache.pop(op_id, None)
    res = _run(op_worker._exec_network_broadcast(pool, None, op_id, OWNER, dict(params)))
    res["_op_id"] = op_id
    return res


def test_a_failed_volley_is_not_reported_as_success(pool):
    """Залп, в котором не ушло ни одного сообщения, не может быть `done`."""
    bc_id = _seed_broadcast(pool, "failed", sent=0, failed=3)
    res = _exec(pool, bc_id)
    assert res["status"] != "done", (
        "операция закрылась успехом, хотя рассылка провалилась: ни сторож "
        f"зависших, ни повтор её уже не коснутся. Итог: {res}")
    assert res["status"] == "failed", f"итог должен быть провалом: {res}"
    assert "не выполнена" in res["summary"], res["summary"]


def test_a_partial_volley_is_reported_partial(pool):
    """Доставлено часть — операция обязана сказать «частично», а не «готово»."""
    bc_id = _seed_broadcast(pool, "partial", sent=2, failed=1)
    res = _exec(pool, bc_id)
    assert res["status"] == "partial", f"итог должен быть частичным: {res}"
    assert res["sent"] == 2 and res["failed"] == 1, res


def test_a_finished_volley_reports_delivery_not_launch(pool):
    """Исход успешного залпа — доставка, а единица прогресса — получатели."""
    bc_id = _seed_broadcast(pool, "done", sent=3, failed=0)
    res = _exec(pool, bc_id)
    assert res["status"] == "done", res
    assert "завершена" in res["summary"], (
        f"операция рапортует о запуске, а не о доставке: {res['summary']!r}")
    row = _run(pool.fetchrow(
        "SELECT total_items, done_items FROM operation_queue WHERE id=$1", res["_op_id"]))
    assert row["total_items"] == 3, (
        "единица прогресса осталась «боты»: владелец видит 1/1 вместо доставки "
        f"по получателям, total_items={row['total_items']}")
    assert row["done_items"] == 3, f"done_items={row['done_items']}"


def test_the_executor_hands_the_volley_over_instead_of_closing_it():
    """Храповик: исполнитель не объявляет исход сам после цикла запуска.

    Тот самый баг и был «вернул `done` сразу после запуска задач». Поэтому
    последним словом функции обязана быть передача залпа сопровождению, а не
    собственный терминальный словарь.
    """
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "_exec_network_broadcast"
    )
    called = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_await_network_broadcasts" in called, (
        "исполнитель сетевой рассылки не сопровождает залп: операция закроется "
        "до первого отправленного сообщения, и остановить её будет нельзя")
    last = fn.body[-1]
    assert isinstance(last, ast.Return) and not isinstance(last.value, ast.Dict), (
        "последним словом исполнителя снова стал собственный исход — значит "
        "операция опять закрывается по факту ЗАПУСКА задач")
