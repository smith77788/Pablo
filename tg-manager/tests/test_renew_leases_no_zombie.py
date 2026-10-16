"""renew_leases продлевает аренду ТОЛЬКО реально удерживаемых аккаунтов.

Живой Postgres. Корень залипания «аккаунт занят операцией» (прогрев день 0/14,
фабрика/операции «все аккаунты заняты»): heartbeat продлевал аренду ВСЕХ
in_operation=TRUE с нашим op_lease_owner — и залипший флаг (в памяти держателя
уже нет) продлевался вечно, аренда никогда не истекала. Тест: флаг БЕЗ живого
держателя в памяти НЕ продлевается (аренда остаётся в прошлом → аккаунт
освобождается по TTL), а реально удерживаемый — продлевается.
"""
from __future__ import annotations

import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_renew_leases_skips_zombie_flags_postgres():
    import asyncio
    import asyncpg

    from services import op_worker as w

    OWNER = 824000

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        _orig_pool = w._db_pool
        w.init_op_worker_pool(pool)

        async def _cl():
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        async def _mk():
            return int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "in_operation,op_lease_owner,op_lease_until) "
                "VALUES($1,$2,'s',TRUE,'active',TRUE,$3, now() - interval '1 hour') RETURNING id",
                OWNER, f"+7977{OWNER%100}{_mk.n:04d}", w._WORKER_ID))
        _mk.n = 0

        try:
            await _cl()
            _mk.n = 1; held = await _mk()      # реально держим в памяти
            _mk.n = 2; zombie = await _mk()    # флаг есть, держателя нет

            async with w._accounts_lock:
                w._accounts_in_use.clear()
                w._accounts_in_use.add(held)

            renewed = await w.renew_leases()
            assert renewed == 1, f"продлить должны РОВНО один (held), а не оба: {renewed}"

            # У held аренда уехала в будущее, у zombie осталась в прошлом.
            held_left = await pool.fetchval(
                "SELECT op_lease_until > now() FROM tg_accounts WHERE id=$1", held)
            zombie_left = await pool.fetchval(
                "SELECT op_lease_until > now() FROM tg_accounts WHERE id=$1", zombie)
            assert held_left is True, "реально удерживаемый аккаунт обязан продлеваться"
            assert zombie_left is False, "залипший флаг НЕ должен продлеваться — иначе вечно занят"

            # Зомби с истёкшей арендой теперь виден как свободный чужому владельцу
            # (warmup/фабрика/операции берут его lease-aware гейтом _db_claim).
            other = await pool.fetchval(
                "SELECT (op_lease_until IS NULL OR op_lease_until < now()) "
                "FROM tg_accounts WHERE id=$1", zombie)
            assert other is True

            await pool.execute("UPDATE tg_accounts SET in_operation=FALSE, op_lease_until=NULL "
                               "WHERE owner_id=$1", OWNER)
            await _cl()
        finally:
            async with w._accounts_lock:
                w._accounts_in_use.clear()
            w._db_pool = _orig_pool
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
