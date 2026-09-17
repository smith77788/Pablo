"""Прогрев ОСВОБОЖДАЕТ аккаунт, даже если сбой случился ДО client.connect().

Живой Postgres. Окно от захвата аккаунта до client.connect() (niche/health/
_make_client) раньше было вне releasing-finally: исключение там (мёртвая сессия,
блип БД) оставляло аккаунт «зомби» в памяти процесса (_accounts_in_use) — и его
не чистили ни renew_leases, ни reconcile (память считает занятым), только
рестарт. Из-за этого аккаунт вечно «занят операцией», план стоит на дне 0/14.
Тест: сбой _make_client в этом окне → аккаунт освобождён, план сдвинут
(last_action_at проставлен, план не залипает как «due навсегда»).
"""
from __future__ import annotations

import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_warmup_releases_account_when_prep_fails_postgres(monkeypatch):
    import asyncio
    import asyncpg

    from services import account_warmer as aw
    from services import account_manager
    from services import op_worker as w

    OWNER = 825000

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        _orig_pool = w._db_pool
        w.init_op_worker_pool(pool)

        async def _cl():
            await pool.execute("DELETE FROM account_warmup_plans WHERE account_id IN "
                               "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        # _make_client падает В ОКНЕ подготовки (до connect) — имитируем мёртвую сессию.
        def _boom(*a, **k):
            raise RuntimeError("dead session (prep window)")
        monkeypatch.setattr(account_manager, "_make_client", _boom)

        try:
            await _cl()
            acc = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "added_at) VALUES($1,$2,'sess',TRUE,'active', now() - interval '10 days') "
                "RETURNING id", OWNER, "+79000009001"))
            plan_id = int(await pool.fetchval(
                "INSERT INTO account_warmup_plans(account_id,owner_id,current_day,target_days,"
                "daily_actions,status,last_action_at) VALUES($1,$2,0,14,5,'active', NULL) "
                "RETURNING id", acc, OWNER))

            async with w._accounts_lock:
                w._accounts_in_use.clear()

            plan = {"id": plan_id, "account_id": acc, "owner_id": OWNER,
                    "current_day": 0, "daily_actions": 5, "target_days": 14}
            await aw.run_daily_warmup(pool, plan)

            # Аккаунт НЕ остался «зомби» в памяти и в БД.
            async with w._accounts_lock:
                leaked = acc in w._accounts_in_use
            assert not leaked, "аккаунт не освобождён в памяти — будет вечно «занят»"
            in_op = await pool.fetchval("SELECT in_operation FROM tg_accounts WHERE id=$1", acc)
            assert in_op in (False, None), "in_operation не снят — аккаунт залипнет"

            # План сдвинулся: last_action_at проставлен (не «due навсегда» → не день 0/14).
            la = await pool.fetchval(
                "SELECT last_action_at FROM account_warmup_plans WHERE id=$1", plan_id)
            assert la is not None, "last_action_at не обновлён — план завис бы на дне 0/14"

            await _cl()
        finally:
            async with w._accounts_lock:
                w._accounts_in_use.clear()
            w._db_pool = _orig_pool
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
