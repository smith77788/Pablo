"""Массовые операции Mini App одним запросом к БД, а не запросом на аккаунт.

Раньше «прогреть все» делал 2 запроса на КАЖДЫЙ аккаунт (до 500 → до 1000
последовательных round-trip внутри одного HTTP-запроса), а «переселить с
мёртвых прокси» — по UPDATE на аккаунт. Здесь проверяем и результат, и то,
что запросов стало константное число, и что чужие данные не задеты.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 994001
OTHER = 994002
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
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
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


class _Recorder:
    """Пишет выполненные запросы, всё остальное проксирует в настоящий пул/соединение."""

    def __init__(self, inner, log: list[str]):
        self._inner = inner
        self._log = log

    def _note(self, q):
        self._log.append(" ".join(str(q).split())[:90])

    async def execute(self, q, *a, **kw):
        self._note(q)
        return await self._inner.execute(q, *a, **kw)

    async def fetch(self, q, *a, **kw):
        self._note(q)
        return await self._inner.fetch(q, *a, **kw)

    async def fetchrow(self, q, *a, **kw):
        self._note(q)
        return await self._inner.fetchrow(q, *a, **kw)

    async def fetchval(self, q, *a, **kw):
        self._note(q)
        return await self._inner.fetchval(q, *a, **kw)

    def transaction(self, *a, **kw):
        return self._inner.transaction(*a, **kw)

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                self._cm = outer._inner.acquire()
                return _Recorder(await self._cm.__aenter__(), outer._log)

            async def __aexit__(self, *exc):
                return await self._cm.__aexit__(*exc)

        return _Ctx()


def _seed_accounts(pool, owner, n, with_session=True):
    async def _s():
        await pool.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", owner)
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
        ids = []
        for i in range(n):
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
                "VALUES($1,$2,$3,TRUE,'active') RETURNING id",
                owner, f"+79{owner % 1000}{i:06d}",
                f"sess{i}" if with_session else None))
        return ids
    return _run(_s())


def test_warmup_bulk_is_batched_and_starts_all(pool):
    from services import mini_app_api as m

    ids = _seed_accounts(pool, OWNER, 7)
    other_ids = _seed_accounts(pool, OTHER, 3)

    queries: list[str] = []
    res = _run(m._warmup_bulk_core(_Recorder(pool, queries), OWNER,
                                   "standard", "mixed", "general"))
    assert res["ok"] is True
    assert res["started"] == 7, res

    # Число запросов не зависит от числа аккаунтов: выборка + CREATE TABLE IF
    # NOT EXISTS + два пакетных INSERT. Раньше было бы 2 запроса на аккаунт.
    assert len(queries) <= 5, queries

    rows = _run(pool.fetch(
        "SELECT account_id, status, plan_type FROM account_warmup_plans WHERE owner_id=$1",
        OWNER))
    assert sorted(r["account_id"] for r in rows) == sorted(ids)
    assert all(r["status"] == "active" and r["plan_type"] == "standard" for r in rows)

    # Чужие аккаунты не задеты.
    foreign = _run(pool.fetch(
        "SELECT 1 FROM account_warmup_plans WHERE account_id = ANY($1::bigint[])",
        other_ids))
    assert not foreign


def test_warmup_bulk_is_idempotent_and_reactivates(pool):
    from services import mini_app_api as m

    ids = _seed_accounts(pool, OWNER, 4)
    _run(m._warmup_bulk_core(pool, OWNER, "standard", "mixed", "general"))
    _run(pool.execute(
        "UPDATE account_warmup_plans SET status='paused', pause_reason='banned' "
        "WHERE owner_id=$1", OWNER))

    # Аккаунты с приостановленным планом снова подходят под выборку.
    res = _run(m._warmup_bulk_core(pool, OWNER, "aggressive", "mixed", "general"))
    assert res["started"] == len(ids)
    rows = _run(pool.fetch(
        "SELECT status, plan_type, pause_reason FROM account_warmup_plans WHERE owner_id=$1",
        OWNER))
    assert len(rows) == len(ids), "дубликаты планов не должны появляться"
    assert all(r["status"] == "active" for r in rows)
    assert all(r["plan_type"] == "aggressive" for r in rows)
    assert all(r["pause_reason"] is None for r in rows), "жалоба снимается с перезапуском"


def _seed_proxies(pool, owner):
    """Один мёртвый прокси с аккаунтами на нём + два живых."""
    async def _s():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
        await pool.execute("DELETE FROM user_proxies WHERE owner_id=$1", owner)
        dead = await pool.fetchval(
            "INSERT INTO user_proxies(owner_id, proxy_url, is_active, is_alive, "
            "consecutive_failures) VALUES($1,$2,TRUE,FALSE,99) RETURNING id",
            owner, f"socks5://dead-{owner}:1080")
        live = []
        for i in range(2):
            live.append(await pool.fetchval(
                "INSERT INTO user_proxies(owner_id, proxy_url, is_active, is_alive, "
                "consecutive_failures) VALUES($1,$2,TRUE,TRUE,0) RETURNING id",
                owner, f"socks5://live{i}-{owner}:1080"))
        accs = []
        for i in range(5):
            accs.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "proxy_id) VALUES($1,$2,$3,TRUE,'active',$4) RETURNING id",
                owner, f"+79{owner % 1000}{i:06d}", f"sess{i}", dead))
        return dead, live, accs
    return _run(_s())


def test_proxy_evacuate_move_is_one_query_and_owner_scoped(pool):
    from services import mini_app_api as m

    dead, live, accs = _seed_proxies(pool, OWNER)
    _dead_o, live_o, accs_other = _seed_proxies(pool, OTHER)

    queries: list[str] = []
    moves = [(acc, live[i % len(live)]) for i, acc in enumerate(accs)]
    moved = _run(m._apply_proxy_moves(_Recorder(pool, queries), OWNER, moves))
    assert moved == len(accs)
    assert len(queries) == 1, "весь план переезда должен уходить одним запросом"

    rows = _run(pool.fetch("SELECT proxy_id FROM tg_accounts WHERE owner_id=$1", OWNER))
    assert all(r["proxy_id"] in live for r in rows)

    # Чужие аккаунты не трогаем, даже если они попали в план переезда.
    foreign_moves = [(acc, live_o[0]) for acc in accs_other]
    assert _run(m._apply_proxy_moves(pool, OWNER, foreign_moves)) == 0
    still = _run(pool.fetch(
        "SELECT proxy_id FROM tg_accounts WHERE id = ANY($1::bigint[])", accs_other))
    assert {r["proxy_id"] for r in still} == {_dead_o}


def test_proxy_evacuate_empty_plan_is_noop(pool):
    from services import mini_app_api as m

    queries: list[str] = []
    assert _run(m._apply_proxy_moves(_Recorder(pool, queries), OWNER, [])) == 0
    assert queries == [], "пустой план не должен ходить в базу"


def test_proxy_evacuate_plan_covers_all_stranded(pool):
    """План эвакуации, который скармливается хелперу, покрывает все зависшие аккаунты."""
    from services.proxy_balancer import plan_evacuation

    dead, live, accs = _seed_proxies(pool, OWNER)
    plan = plan_evacuation(accs, [(pid, 0) for pid in live])
    assert not plan["stranded"]
    assert sorted(a for a, _p in plan["moves"]) == sorted(accs)
