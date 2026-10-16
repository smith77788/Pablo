"""Новая попытка операции не наследует чужой отсчёт ожидания флота.

`operation_queue.acct_wait_since` — отметка «с какого момента операция ждёт
свободные аккаунты». По ней `_requeue_op_no_accounts` через
`_ACCT_WAIT_MAX_MIN` (20 минут) ЗАКРЫВАЕТ операцию текстом «все аккаунты N мин
были заняты другими операциями». Поэтому каждый возврат в очередь по ДРУГОЙ
причине эту отметку снимает: иначе следующая попытка начинается с чужим
отсчётом и первая же секунда занятого флота её проваливает.

Так делали одиннадцать возвратов из двенадцати. Выпадал возврат по месту в
семафоре владельца (`_release_op_for_owner_limit`): причина там чисто
внутренняя — соседняя операция того же владельца ещё доигрывает finally, — и
операция, однажды прождавшая флот 19 минут, получала «не запустилась: все
аккаунты 20 мин были заняты» на прогоне, где она не ждала вовсе.

Нужен живой Postgres: проверяется, что` NULL` действительно лёг в строку, а
заглушка пула SQL не исполняет. Храповик рядом — текстовый, он про остальные
одиннадцать и про следующий возврат, который кто-то напишет.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993422

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
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


def test_the_semaphore_requeue_drops_the_inherited_wait(pool):
    """Возврат по занятому семафору снимает отметку ожидания флота."""
    from services import op_worker

    op_id = int(_run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
        "                            label, started_at, acct_wait_since) "
        "VALUES($1,'mass_invite','running',$2,10,'wait test', now(), "
        "       now() - make_interval(mins => 19)) RETURNING id",
        OWNER, json.dumps({}))))

    _run(op_worker._release_op_for_owner_limit(pool, op_id))

    row = _run(pool.fetchrow(
        "SELECT status, acct_wait_since FROM operation_queue WHERE id=$1", op_id))
    assert row["status"] == "pending", f"операция не вернулась в очередь: {row['status']}"
    assert row["acct_wait_since"] is None, (
        "новая попытка унаследовала отсчёт ожидания флота (19 минут): первая "
        "же секунда занятого флота закроет её как «не дождалась 20 минут»")


def test_every_requeue_decides_about_the_wait_marker():
    """Храповик: любой возврат в очередь обязан РЕШИТЬ про отметку ожидания.

    Решение бывает двух видов: снять (новая попытка) или продлить через
    COALESCE (то самое ожидание флота и продолжается). Молчание — это
    унаследованный чужой отсчёт, то есть провал операции за ожидание, которого
    не было.
    """
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    writers = ("_safe_execute", "_retrying_execute", "_op_closed_by_us")
    silent = []
    seen = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in writers):
            continue
        idx = 2 if node.func.id == "_op_closed_by_us" else 1
        if len(node.args) <= idx:
            continue
        sql = re.sub(r"\s+", " ", ast.get_source_segment(src, node.args[idx]) or "")
        if "operation_queue" not in sql or not re.search(r"status\s*=\s*'pending'", sql):
            continue
        seen += 1
        if "acct_wait_since" not in sql:
            silent.append((node.lineno, sql[:90]))
    assert seen >= 8, f"пробник не нашёл возвратов в очередь ({seen}) — он сломан"
    assert not silent, (
        "эти возвраты в очередь оставляют отметку ожидания флота как есть — "
        f"следующая попытка начнётся с чужим отсчётом: {silent}")


def test_the_wait_marker_detector_bites():
    """Самопроверка: молчаливый возврат детектор обязан найти."""
    sick = ("await _safe_execute(pool, \"UPDATE operation_queue SET status='pending', "
            "started_at=NULL WHERE id=$1\", op_id)")
    node = ast.parse(sick).body[0].value.value   # Await → Call
    sql = re.sub(r"\s+", " ", ast.get_source_segment(sick, node.args[1]) or "")
    assert "operation_queue" in sql and re.search(r"status\s*=\s*'pending'", sql)
    assert "acct_wait_since" not in sql
