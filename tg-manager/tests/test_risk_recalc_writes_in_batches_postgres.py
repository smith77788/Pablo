"""Пересчёт риска флота и «Исправить версии» пишут пачкой, а не запросом на аккаунт.

`physics_engine.compute_owner` идёт на каждом круге фонового цикла и на
каждом проходе щита и писал оценку каждого аккаунта отдельным INSERT — на
флоте в тысячи аккаунтов это тысячи round-trip'ов за круг.
`account_bulk_attrs.regenerate_versions` (кнопка над выбором аккаунтов)
делал так же. Здесь на живой базе: результат прежний, а запросов —
единицы; и пачка, которую база отбила, дописывается построчно.

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/<база>.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_418
N = 1200


class _Counting:
    """Пул, который считает запросы записи."""

    def __init__(self, pool, fail_many=False):
        self._p = pool
        self.execute_calls = 0
        self.many_calls = 0
        self.fail_many = fail_many

    async def fetch(self, *a, **k):
        return await self._p.fetch(*a, **k)

    async def execute(self, *a, **k):
        self.execute_calls += 1
        return await self._p.execute(*a, **k)

    async def executemany(self, *a, **k):
        self.many_calls += 1
        if self.fail_many:
            raise RuntimeError("пачка отбита")
        return await self._p.executemany(*a, **k)


def _go(body):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _main():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            await _clean(p)
            await body(p)
        finally:
            await _clean(p)
            await p.close()

    asyncio.run(_main())


async def _clean(p):
    await p.execute(
        "DELETE FROM account_risk_scores WHERE account_id IN "
        "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)


async def _fleet(p, n=N):
    await p.execute(
        "INSERT INTO tg_accounts (owner_id, phone, session_str, is_active) "
        "SELECT $1, '+7418' || g, 's' || g, TRUE FROM generate_series(1, $2) g",
        OWNER, n)
    return [r["id"] for r in await p.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 ORDER BY id", OWNER)]


def test_fleet_risk_is_written_in_a_few_batches():
    from services import physics_engine as pe

    async def body(pool):
        ids = await _fleet(pool)
        flooded = ids[7]
        for _ in range(3):
            await pool.execute(
                "INSERT INTO account_flood_log (account_id, operation, flood_seconds) "
                "VALUES ($1, 'invite', 1800)", flooded)
        cp = _Counting(pool)
        n = await pe.compute_owner(cp, OWNER)
        assert n == N
        assert cp.execute_calls == 0
        assert cp.many_calls == -(-N // pe._UPSERT_CHUNK)
        got = {r["account_id"]: r["risk_score"] for r in await pool.fetch(
            "SELECT account_id, risk_score FROM account_risk_scores "
            "WHERE account_id = ANY($1::bigint[])", ids)}
        assert set(got) == set(ids)
        calm = got[ids[0]]
        assert got[flooded] > calm      # флуд поднял риск именно у своего

        # Повтор не плодит строк — это upsert.
        assert await pe.compute_owner(pool, OWNER) == N
        assert await pool.fetchval(
            "SELECT count(*) FROM account_risk_scores "
            "WHERE account_id = ANY($1::bigint[])", ids) == N

    _go(body)


def test_rejected_batch_falls_back_to_rows():
    from services import physics_engine as pe

    async def body(pool):
        ids = await _fleet(pool, 30)
        cp = _Counting(pool, fail_many=True)
        assert await pe.compute_owner(cp, OWNER) == 30
        assert cp.execute_calls == 30
        assert await pool.fetchval(
            "SELECT count(*) FROM account_risk_scores "
            "WHERE account_id = ANY($1::bigint[])", ids) == 30

    _go(body)


def test_regenerate_versions_is_one_batch_with_own_fingerprints():
    from services import account_bulk_attrs as ab

    async def body(pool):
        ids = await _fleet(pool, 300)
        cp = _Counting(pool)
        assert await ab.regenerate_versions(cp, OWNER, ids) == 300
        assert cp.execute_calls == 0 and cp.many_calls == 1
        rows = await pool.fetch(
            "SELECT device_model, system_version, app_version FROM tg_accounts "
            "WHERE owner_id=$1", OWNER)
        assert all(r["device_model"] for r in rows)
        # У пачки нет одной сигнатуры на всех.
        assert len({tuple(r) for r in rows}) > 1
        # Чужой аккаунт не трогается: owner_id стоит в самой записи.
        other = await pool.fetchval(
            "INSERT INTO tg_accounts (owner_id, phone, session_str, is_active) "
            "VALUES ($1, '+7418999', 's', TRUE) RETURNING id", OWNER + 1)
        try:
            assert await ab.regenerate_versions(pool, OWNER, [other]) == 0
            assert await pool.fetchval(
                "SELECT device_model FROM tg_accounts WHERE id=$1", other) is None
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE id=$1", other)

    _go(body)
