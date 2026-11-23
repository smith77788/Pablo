"""Новые запросы уведомлений — против НАСТОЯЩЕЙ базы.

Разбор текста SQL не доказывает, что Postgres его примет (CLAUDE.md:
`$1::bigint[]` против uuid). Здесь: маршруты каналов уведомлений
(schema_v384) и `record_user_activity` — «когда человек писал боту до этого».
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 993871
_LOOP = None


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
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    _run(p.execute("DELETE FROM bot_notify_routes WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM user_activity WHERE bot_id=$1", OWNER))
    _run(p.execute(
        "INSERT INTO managed_bots(token, bot_id, added_by) VALUES('x', $1, $1) "
        "ON CONFLICT DO NOTHING", OWNER))
    yield p
    _run(p.close())


def test_routes_crud_and_matching(pool):
    from services import bot_notify_routes as R

    a, _ = R.normalize({"name": "Крипто", "chat_id": -1001, "events": ["start"], "bot_ids": [5, 0]})
    b, _ = R.normalize({"name": "Новости", "chat_id": -1002, "events": ["start", "join_request"]})
    ida = _run(R.save_route(pool, OWNER, a, "Канал А"))
    idb = _run(R.save_route(pool, OWNER, b, "Канал Б"))
    routes = _run(R.list_routes(pool, OWNER))
    assert [r["id"] for r in routes] == [ida, idb]
    assert routes[0]["bot_ids"] == [0, 5] and routes[1]["bot_ids"] is None
    assert R.matches(routes[0], 5, "start") and not R.matches(routes[0], 6, "start")
    assert R.matches(routes[1], 6, "join_request")
    # Правка чужого маршрута не проходит.
    assert _run(R.save_route(pool, OWNER + 1, a, "x", ida)) is None
    a["events"] = ["join_request"]
    assert _run(R.save_route(pool, OWNER, a, "Канал А", ida)) == ida
    assert _run(R.list_routes(pool, OWNER))[0]["events"] == ["join_request"]
    _run(R._mark(pool, idb, "ошибка"))
    assert _run(R.list_routes(pool, OWNER))[1]["last_error"] == "ошибка"
    assert not _run(R.delete_route(pool, OWNER + 1, idb))
    assert _run(R.delete_route(pool, OWNER, idb))
    assert len(_run(R.list_routes(pool, OWNER))) == 1


def test_record_user_activity_returns_previous_visit(pool):
    from database import db

    is_new, prev = _run(db.record_user_activity(pool, OWNER, 42))
    assert is_new is True and prev is None
    _run(pool.execute(
        "UPDATE user_activity SET last_seen = now() - interval '3 days' "
        "WHERE bot_id=$1 AND user_id=42", OWNER))
    is_new, prev = _run(db.record_user_activity(pool, OWNER, 42))
    assert is_new is False and prev is not None
    from services import auto_responder as ar
    assert ar._returned_after_silence(prev)
    # и сразу повторный — уже не «вернулся»
    _is_new, prev2 = _run(db.record_user_activity(pool, OWNER, 42))
    assert not ar._returned_after_silence(prev2)
    assert _run(db.upsert_user_activity(pool, OWNER, 43)) is True
