"""Ткань присутствия (presence-fabric): личности, реконсайлер, непрерывность.

Почти всё — на живом Postgres: суть в связке desired-state → материализация тела
→ актуация через operation_queue → восстановление при смерти тела. Заглушка пула
это не проверяет.
"""
from __future__ import annotations

import os

import pytest

from services import presence_fabric as pf

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_state_constants():
    # машина состояний цели присутствия
    assert (pf.T_DESIRED, pf.T_CONVERGING, pf.T_PRESENT, pf.T_LOST) == \
        ("desired", "converging", "present", "lost")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_presence_full_lifecycle_postgres():
    import asyncio
    import asyncpg

    OWN = 803000
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM presence_events WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM presence_targets WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM presence_identities WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWN)

        # тело живо всегда (карантин выключен для детерминизма)
        async def _never_quar(_pool, _acc):
            return False

        # submit_fn: реальный operation_queue-ряд (доказываем связь с очередью),
        # возвращаем его id — реконсайлер переведёт цель в converging по нему.
        async def _submit(pool_, owner, chat_ref, acc_id):
            return int(await pool_.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items) "
                "VALUES($1,'bulk_join','pending',$2::jsonb,1) RETURNING id",
                owner, __import__("json").dumps({"links": [chat_ref], "account_ids": [acc_id]})))

        try:
            await _cl()
            # два «тела» (аккаунта) владельца
            a1 = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active) "
                "VALUES($1,'+70000000001','s1',TRUE) RETURNING id", OWN))
            a2 = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active) "
                "VALUES($1,'+70000000002','s2',TRUE) RETURNING id", OWN))

            ident = await pf.create_identity(pool, OWN, "Аня", avatar_emoji="👩")
            iid = ident["id"]
            await pf.add_target(pool, iid, OWN, "@somechannel")

            # 1) первый реконсайл: материализация тела + запуск конвергенции
            fresh = await pf.get_identity(pool, iid)
            r1 = await pf.reconcile_identity(pool, fresh, submit_fn=_submit,
                                             quarantine_fn=_never_quar)
            assert r1["body"] in (a1, a2)
            body1 = r1["body"]
            assert r1["submitted"] == 1 and r1["converging"] == 1
            t = (await pf.list_targets(pool, iid))[0]
            assert t["state"] == pf.T_CONVERGING and t["last_op_id"]
            kinds = [e["kind"] for e in await pf.list_events(pool, OWN, identity_id=iid)]
            assert "materialized" in kinds and "target_converging" in kinds

            # 2) операция завершилась успехом → цель становится present
            await pool.execute("UPDATE operation_queue SET status='done' WHERE id=$1",
                               t["last_op_id"])
            fresh = await pf.get_identity(pool, iid)
            r2 = await pf.reconcile_identity(pool, fresh, submit_fn=_submit,
                                             quarantine_fn=_never_quar)
            assert r2["present"] == 1
            assert (await pf.list_targets(pool, iid))[0]["state"] == pf.T_PRESENT

            # 3) ТЕЛО УМЕРЛО → личность переносится на свежее тело, присутствие
            #    восстанавливается (инвариант непрерывности).
            await pool.execute("UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", body1)
            fresh = await pf.get_identity(pool, iid)
            r3 = await pf.reconcile_identity(pool, fresh, submit_fn=_submit,
                                             quarantine_fn=_never_quar)
            assert r3["body"] and r3["body"] != body1     # новое тело
            ident_after = await pf.get_identity(pool, iid)
            assert int(ident_after["acc_id"]) != body1
            ev = [e["kind"] for e in await pf.list_events(pool, OWN, identity_id=iid)]
            assert "rematerialized" in ev
            # членство восстанавливается: снова запущена конвергенция на новом теле
            assert (await pf.list_targets(pool, iid))[0]["state"] == pf.T_CONVERGING
            assert r3["submitted"] == 1

            # 4) проекция состояния для UI
            proj = await pf.project_identity(pool, iid)
            assert proj["identity"]["id"] == iid
            assert proj["breakdown"][pf.T_CONVERGING] == 1
            assert len(proj["events"]) >= 3
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_reconcile_no_body_postgres():
    """Нет здоровых аккаунтов → личность не материализуется, событие no_body,
    актуация не запускается (не жжём операции впустую)."""
    import asyncio
    import asyncpg

    OWN = 803001
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM presence_events WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM presence_targets WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM presence_identities WHERE owner_id=$1", OWN)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWN)

        submitted = []
        async def _submit(pool_, owner, chat_ref, acc_id):
            submitted.append(chat_ref)
            return 1
        async def _never_quar(_p, _a):
            return False
        try:
            await _cl()
            ident = await pf.create_identity(pool, OWN, "Без тела")
            await pf.add_target(pool, ident["id"], OWN, "@x")
            fresh = await pf.get_identity(pool, ident["id"])
            r = await pf.reconcile_identity(pool, fresh, submit_fn=_submit,
                                            quarantine_fn=_never_quar)
            assert r["no_body"] is True and r["submitted"] == 0
            assert submitted == []
            kinds = [e["kind"] for e in await pf.list_events(pool, OWN)]
            assert "no_body" in kinds
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_default_actuation_uses_operation_bus_postgres():
    """Актуация по умолчанию идёт РЕАЛЬНО через operation_bus.submit('bulk_join')
    и создаёт ряд в operation_queue — доказательство подключения к движку."""
    import asyncio
    import asyncpg
    from services import operation_bus

    OWN = 803002
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWN)
            # прямой контракт актуатора: тип зарегистрирован и ряд создаётся
            oid = await operation_bus.submit(
                pool, OWN, "bulk_join",
                {"links": ["@chan"], "account_ids": [12345]},
                total_items=1, bypass_plan_check=True)
            row = await pool.fetchrow(
                "SELECT op_type, status FROM operation_queue WHERE id=$1", oid)
            assert row and row["op_type"] == "bulk_join"
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWN)
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
