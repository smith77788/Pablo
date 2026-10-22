"""Пауза плана прогрева не отменяется концом того же прогона.

РАЗРЫВ. Хвост `_run_daily_warmup_impl` писал план безусловно:

    SET current_day=$1, status=$2, ... WHERE id=$3     -- $2: 'active'/'completed'

а пауза ставится ПО ХОДУ этого же прогона: `_pause_plan(plan_id, 'banned')`
на бане аккаунта и `_pause_plan(plan_id, 'restricted')` на spam-ограничении —
то есть ровно там, где комментарий в коде говорит «не добивать флагнутый
аккаунт». Прогон доходил до хвоста и возвращал план в 'active': следующий круг
снова грел забаненный или ограниченный аккаунт. Пауза из Mini App и отмена из
бота отменялись так же.

ЧТО ПРОВЕРЯЕМ. Запрос берётся ИЗ ИСХОДНИКА и выполняется на живом Postgres:
по плану в 'paused' статус обязан остаться 'paused', а день — продвинуться
(прогресс не статус). По активному плану запрос работает как раньше. Плюс
храповик: цикл шагов обязан спрашивать статус у базы, иначе пауза владельца
не останавливает уже идущие действия.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993441
ACCOUNT = 777001

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _tail_query() -> str:
    from services import account_warmer

    src = inspect.getsource(account_warmer._run_daily_warmup_impl)
    m = re.search(r'"""(UPDATE account_warmup_plans.*?WHERE id=\$3)"""', src, re.S)
    assert m, "запрос обновления плана не найден в исходнике"
    return m.group(1)


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
    _run(p.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", OWNER))
    yield p
    _run(p.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", OWNER))
    _run(p.close())


def _plan(pool, status: str) -> int:
    _run(pool.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", OWNER))
    return _run(pool.fetchval(
        """INSERT INTO account_warmup_plans(owner_id, account_id, current_day,
                                            target_days, daily_actions, status)
           VALUES($1,$2,3,14,5,$3) RETURNING id""",
        OWNER, ACCOUNT, status))


def test_a_paused_plan_is_not_revived_by_the_end_of_the_run(pool):
    plan_id = _plan(pool, "paused")
    _run(pool.execute(_tail_query(), 4, "active", plan_id, None))

    row = _run(pool.fetchrow(
        "SELECT status, current_day FROM account_warmup_plans WHERE id=$1", plan_id))
    assert row["status"] == "paused", (
        "план вернулся в работу после паузы — следующий круг снова греет "
        "забаненный аккаунт")
    assert row["current_day"] == 4, "прогресс дня — не статус, он писаться обязан"


def test_a_cancelled_plan_is_not_completed_either(pool):
    plan_id = _plan(pool, "cancelled")
    _run(pool.execute(_tail_query(), 14, "completed", plan_id, None))

    row = _run(pool.fetchrow(
        "SELECT status, completed_at FROM account_warmup_plans WHERE id=$1", plan_id))
    assert row["status"] == "cancelled", "отменённый владельцем план «завершился»"
    assert row["completed_at"] is None, "у отменённого плана проставлена дата финала"


def test_an_active_plan_still_advances(pool):
    plan_id = _plan(pool, "active")
    _run(pool.execute(_tail_query(), 14, "completed", plan_id, "all_failed"))

    row = _run(pool.fetchrow(
        "SELECT status, current_day, completed_at, last_skip_reason "
        "FROM account_warmup_plans WHERE id=$1", plan_id))
    assert row["status"] == "completed", "обычный путь сломан"
    assert row["current_day"] == 14
    assert row["completed_at"] is not None
    assert row["last_skip_reason"] == "all_failed"


def test_the_step_loop_asks_the_database_whether_it_was_stopped():
    """Иначе пауза владельца не останавливает уже идущие действия."""
    from services import account_warmer

    path = inspect.getsourcefile(account_warmer)
    tree = ast.parse(open(path, encoding="utf-8").read())
    impl = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and n.name == "_run_daily_warmup_impl"), None)
    assert impl is not None, "исполнитель дневного прогрева не найден"
    loop = None
    for node in ast.walk(impl):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if (isinstance(it, ast.Call)
                and getattr(it.func, "id", None) == "range"
                and any(getattr(a, "id", None) == "total_steps" for a in it.args)):
            loop = node
            break
    assert loop is not None, "цикл шагов прогрева не найден"
    called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
              for c in ast.walk(loop) if isinstance(c, ast.Call)}
    assert "_plan_stopped_by_owner" in called, (
        "цикл шагов не спрашивает статус плана — пауза владельца не остановит "
        "уже идущие действия живым аккаунтом")
