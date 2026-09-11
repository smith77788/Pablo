"""Прогрев не должен молча стоять из-за ПРОТЁКШЕГО флага занятости.

Регресс: цикл прогрева выбирал планы по `in_operation=FALSE`, игнорируя аренду.
Если операция упала и оставила in_operation=TRUE с истёкшей арендой, аккаунт
числился занятым вечно — прогрев стоял (день 0/14, «занят операцией», часы без
действий). Атомарный захват и так считает истёкшую/пустую аренду свободной —
предфильтр цикла выровнен с этим. Аккаунт с ЖИВОЙ арендой по-прежнему пропускаем.
"""
from __future__ import annotations

import os

import pytest

from services import account_warmer as aw

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_due_plans_ignore_stale_lease_postgres():
    import asyncio
    import asyncpg

    OWN = 804000
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWN)

        async def _mk_acc(phone, in_op, lease_sql):
            return int(await pool.fetchval(
                f"INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
                f"in_operation,op_lease_until) VALUES($1,$2,'s',TRUE,$3,{lease_sql}) "
                f"RETURNING id", OWN, phone, in_op))

        async def _mk_plan(acc_id):
            await pool.execute(
                "INSERT INTO account_warmup_plans(owner_id,account_id,status,last_action_at) "
                "VALUES($1,$2,'active',NULL)", OWN, acc_id)

        try:
            await _cl()
            a_free = await _mk_acc("+70000000001", False, "NULL")
            a_stale = await _mk_acc("+70000000002", True, "now() - interval '1 hour'")
            a_null = await _mk_acc("+70000000003", True, "NULL")  # legacy: флаг без аренды
            a_busy = await _mk_acc("+70000000004", True, "now() + interval '1 hour'")
            for a in (a_free, a_stale, a_null, a_busy):
                await _mk_plan(a)

            rows = await aw._select_due_plans(pool)
            got = {int(r["account_id"]) for r in rows if int(r["owner_id"]) == OWN}

            # свободный, протёкшая аренда и legacy-флаг без аренды — берём
            assert a_free in got
            assert a_stale in got   # ← падало бы без фикса (числился занятым вечно)
            assert a_null in got    # ← и это тоже
            # аккаунт с ЖИВОЙ арендой (реальная операция) — НЕ трогаем (анти-бан)
            assert a_busy not in got
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
