"""Операции с базами аудитории (merge/exclude/dedup) по НАСТОЯЩЕМУ Postgres.

Set-операции над parsed_audiences — чистый SQL, но с нетривиальным биндингом
(ANY($::bigint[]), DISTINCT ON, NOT IN подзапрос) и инвариантом уникальности
(owner_id, source_id, tg_user_id). Заглушка пула типы не проверяет, поэтому
корректность проверяем на живой БД: реальные счётчики множеств + скоуп-гард.

Запуск — см. docstring test_invite_e2e_postgres.py (тот же стенд/DSN).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 991201
OTHER = 991202

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _Stand:
    def __init__(self, pool):
        self.pool = pool

    async def make_run(self, owner: int, ref: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO parser_runs(owner_id, source_type, source_ref, parse_type, status) "
            "VALUES($1,'channel',$2,'members','done') RETURNING id", owner, ref)

    async def add_members(self, owner: int, run_id: int, user_ids, *, source_id=None):
        # source_id по умолчанию = run_id (как у реального парса — одна база один источник)
        sid = run_id if source_id is None else source_id
        for uid in user_ids:
            await self.pool.execute(
                "INSERT INTO parsed_audiences(owner_id,source_type,source_id,parse_run_id,"
                "tg_user_id,username) VALUES($1,'channel',$2,$3,$4,$5) "
                "ON CONFLICT (owner_id, source_id, tg_user_id) DO NOTHING",
                owner, sid, run_id, uid, f"u{uid}")

    async def members(self, run_id: int) -> set:
        rows = await self.pool.fetch(
            "SELECT tg_user_id FROM parsed_audiences WHERE parse_run_id=$1", run_id)
        return {r["tg_user_id"] for r in rows}

    async def clean(self):
        await self.pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id = ANY($1::bigint[])", [OWNER, OTHER])
        await self.pool.execute(
            "DELETE FROM parser_runs WHERE owner_id = ANY($1::bigint[])", [OWNER, OTHER])


@pytest.fixture(scope="module")
def stand():
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
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        pool = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    s = _Stand(pool)
    _run(s.clean())
    yield s
    _run(pool.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def test_merge_is_union_of_unique_users(stand):
    from services import audience_ops
    _run(stand.clean())
    a = _run(stand.make_run(OWNER, "A"))
    b = _run(stand.make_run(OWNER, "B"))
    _run(stand.add_members(OWNER, a, [1, 2, 3]))
    _run(stand.add_members(OWNER, b, [3, 4, 5]))  # 3 пересекается
    res = _run(audience_ops.merge_runs(stand.pool, OWNER, [a, b]))
    assert res["count"] == 5  # {1,2,3,4,5}
    assert _run(stand.members(res["run_id"])) == {1, 2, 3, 4, 5}


def test_exclude_removes_minus_base_members(stand):
    from services import audience_ops
    _run(stand.clean())
    a = _run(stand.make_run(OWNER, "base"))
    b = _run(stand.make_run(OWNER, "sent"))
    _run(stand.add_members(OWNER, a, [1, 2, 3, 4]))
    _run(stand.add_members(OWNER, b, [2, 4]))  # уже охваченные
    res = _run(audience_ops.exclude_runs(stand.pool, OWNER, a, [b]))
    assert res["count"] == 2
    assert _run(stand.members(res["run_id"])) == {1, 3}


def test_dedup_collapses_same_user_across_sources(stand):
    from services import audience_ops
    _run(stand.clean())
    a = _run(stand.make_run(OWNER, "dirty"))
    # один и тот же tg_user_id в одной базе, но из разных source_id (unique-индекс
    # это допускает) → реальный дубль для дедупа
    _run(stand.add_members(OWNER, a, [10, 11], source_id=a))
    _run(stand.add_members(OWNER, a, [11, 12], source_id=a + 100000))
    assert _run(stand.members(a)) == {10, 11, 12}
    dup_rows = _run(stand.pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE parse_run_id=$1", a))
    assert dup_rows == 4  # 11 дважды
    res = _run(audience_ops.dedup_run(stand.pool, OWNER, a))
    assert res["count"] == 3  # {10,11,12}
    new_rows = _run(stand.pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE parse_run_id=$1", res["run_id"]))
    assert new_rows == 3  # дублей больше нет


def test_ops_are_non_destructive(stand):
    from services import audience_ops
    _run(stand.clean())
    a = _run(stand.make_run(OWNER, "A"))
    b = _run(stand.make_run(OWNER, "B"))
    _run(stand.add_members(OWNER, a, [1, 2]))
    _run(stand.add_members(OWNER, b, [2, 3]))
    _run(audience_ops.merge_runs(stand.pool, OWNER, [a, b]))
    # исходные базы не тронуты
    assert _run(stand.members(a)) == {1, 2}
    assert _run(stand.members(b)) == {2, 3}


def test_scope_guard_rejects_foreign_bases(stand):
    from services import audience_ops
    _run(stand.clean())
    mine = _run(stand.make_run(OWNER, "mine"))
    theirs = _run(stand.make_run(OTHER, "theirs"))
    _run(stand.add_members(OWNER, mine, [1, 2]))
    _run(stand.add_members(OTHER, theirs, [3, 4]))
    # чужая база отфильтровывается: остаётся <2 своих → отказ
    with pytest.raises(audience_ops.AudienceOpError):
        _run(audience_ops.merge_runs(stand.pool, OWNER, [mine, theirs]))
    # exclude с чужой минус-базой: минус игнорируется как не-своя
    with pytest.raises(audience_ops.AudienceOpError):
        _run(audience_ops.exclude_runs(stand.pool, OWNER, mine, [theirs]))


def test_empty_and_bad_input_refused(stand):
    from services import audience_ops
    _run(stand.clean())
    with pytest.raises(audience_ops.AudienceOpError):
        _run(audience_ops.merge_runs(stand.pool, OWNER, []))
    with pytest.raises(audience_ops.AudienceOpError):
        _run(audience_ops.dedup_run(stand.pool, OWNER, 99999999))
