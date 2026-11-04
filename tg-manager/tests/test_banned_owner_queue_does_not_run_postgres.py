"""Операции забаненного владельца не запускаются поллером очереди.

Бан закрывал бот, мини-апп и ключи API, но операции, поставленные до бана
(рассылки, инвайты, публикации), поллер продолжал поднимать: забаненный за спам
владелец спамил дальше очередью. Теперь отбор пропускает забаненных; строки
остаются в очереди и пойдут после разбана.

Живой Postgres: проверяется семантика запроса отбора. Тест падает без фикса.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 991884
OTHER_OWNER = 991885
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    from services import op_worker

    _run(pool.execute(
        "DELETE FROM operation_queue WHERE owner_id >= 990000 "
        "AND status IN ('pending','running')"))
    _run(pool.execute(
        "DELETE FROM platform_users WHERE user_id = ANY($1::bigint[])",
        [OWNER, OTHER_OWNER]))
    op_worker._active_op_ids.clear()
    op_worker._shutting_down = False
    yield
    _run(pool.execute(
        "DELETE FROM platform_users WHERE user_id = ANY($1::bigint[])",
        [OWNER, OTHER_OWNER]))


@pytest.fixture
def started(monkeypatch):
    from services import op_worker

    seen: list[int] = []

    async def _fake(pool, bot, row):
        seen.append(int(row["id"]))

    monkeypatch.setattr(op_worker, "_run_op_task_guarded", _fake)
    return seen


async def _op(pool, owner: int) -> int:
    return await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items) "
        "VALUES($1,'mass_invite',$2::jsonb,'pending',10) RETURNING id",
        owner, json.dumps({}))


def test_banned_owner_operation_waits_and_others_run(pool, started):
    from services import op_worker

    _run(pool.execute(
        "INSERT INTO platform_users(user_id, is_banned) VALUES($1, true), ($2, false)",
        OWNER, OTHER_OWNER))
    banned_op = _run(_op(pool, OWNER))
    ok_op = _run(_op(pool, OTHER_OWNER))
    _run(op_worker._process_pending(pool, None))
    assert ok_op in started
    assert banned_op not in started, "операция забаненного владельца запустилась"
    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", banned_op)) == "pending"

    _run(pool.execute("UPDATE platform_users SET is_banned=false WHERE user_id=$1", OWNER))
    _run(op_worker._process_pending(pool, None))
    assert banned_op in started, "после разбана операция не пошла"
