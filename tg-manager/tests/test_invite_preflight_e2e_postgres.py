"""Пре-флайт готовности флота к инвайту (агрегатор по живому Postgres)."""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 989501
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
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _seed(pool, n):
    async def _s():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        ids = []
        for i in range(n):
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "first_name) VALUES($1,$2,$3,TRUE,'active','A') RETURNING id",
                OWNER, f"+7999{OWNER%100}{i:05d}", f"s{i}"))
        return ids
    return _run(_s())


def _mk_checker(state_by_id):
    async def checker(session, acc, group):
        return state_by_id[int(acc["id"])]
    return checker


def test_aggregates_states_and_verdict(pool):
    from services.invite_preflight import run_preflight
    ids = _seed(pool, 5)
    states = {
        ids[0]: {"state": "admin", "can_promote": True, "can_invite": True},
        ids[1]: {"state": "member", "can_invite": False},
        ids[2]: {"state": "not_member"},
        ids[3]: {"state": "no_connect", "error": "net"},
        ids[4]: {"state": "member", "can_invite": True},
    }
    rep = _run(run_preflight(pool, OWNER, "@g", checker=_mk_checker(states)))
    c = rep["counts"]
    assert rep["total"] == 5
    assert c["admin"] == 1 and c["member"] == 2 and c["not_member"] == 1 and c["no_connect"] == 1
    assert rep["ready"] == 3            # admin + 2 members
    # can_invite: admin + member-с-правом = 2
    assert rep["can_invite"] == 2
    # вердикт упоминает и не подключившихся, и наличие админа
    assert "не подключил" in rep["verdict"].lower()
    assert "админ" in rep["verdict"].lower()


def test_no_admin_verdict_warns(pool):
    from services.invite_preflight import run_preflight
    ids = _seed(pool, 3)
    states = {i: {"state": "member", "can_invite": False} for i in ids}
    rep = _run(run_preflight(pool, OWNER, "@g", checker=_mk_checker(states)))
    assert rep["admins"] == 0
    assert "нет ни одного админа" in rep["verdict"].lower()


def test_all_ready_verdict(pool):
    from services.invite_preflight import run_preflight
    ids = _seed(pool, 2)
    states = {ids[0]: {"state": "admin", "can_promote": True, "can_invite": True},
              ids[1]: {"state": "member", "can_invite": True}}
    rep = _run(run_preflight(pool, OWNER, "@g", checker=_mk_checker(states)))
    assert rep["counts"]["no_connect"] == 0 and rep["counts"]["not_member"] == 0
    assert "есть 1 админ" in rep["verdict"].lower() or "готов" in rep["verdict"].lower()


def test_group_bad_for_all(pool):
    from services.invite_preflight import run_preflight
    ids = _seed(pool, 3)
    states = {i: {"state": "group_bad", "error": "no access"} for i in ids}
    rep = _run(run_preflight(pool, OWNER, "@g", checker=_mk_checker(states)))
    assert "группа недоступна" in rep["verdict"].lower()


def test_selected_account_ids_subset(pool):
    from services.invite_preflight import run_preflight
    ids = _seed(pool, 4)
    states = {i: {"state": "member", "can_invite": True} for i in ids}
    rep = _run(run_preflight(pool, OWNER, "@g", account_ids=ids[:2],
                             checker=_mk_checker(states)))
    assert rep["total"] == 2          # только выбранные
