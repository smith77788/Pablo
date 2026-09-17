"""Массовое создание каналов/групп СРАЗУ В НЕСКОЛЬКИХ аккаунтах по параметрам.

Живой Postgres: суть — исполнитель bulk_create_channels в multi-режиме создаёт
channel_count штук НА КАЖДЫЙ выбранный аккаунт, уважает name_mode (нумерация/по
аккаунту/одинаково) и is_group (канал против группы). Telethon-создание канала
застаблено — проверяем оркестрацию и контракт параметров, на которые опирается
API мини-аппа (account_ids/title/channel_count/name_mode/is_group).
"""
from __future__ import annotations

import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_bulk_create_channels_multi_per_account_postgres(monkeypatch):
    import asyncio
    import asyncpg

    from services import op_worker as w
    from services import account_manager, session_simulator

    OWNER = 823000

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        _orig_pool = w._db_pool          # вернём в finally: init ставит ГЛОБАЛЬНЫЙ
        w.init_op_worker_pool(pool)      # пул op_worker — иначе закрытый пул отравит
        #                                  следующие тесты («pool is closed»).

        calls = []          # (title, megagroup)
        seq = {"n": 5000}

        async def fake_create(session_string, title, about="", megagroup=False, _acc=None):
            seq["n"] += 1
            calls.append((title, bool(megagroup)))
            return {"channel_id": seq["n"], "title": title,
                    "type": "group" if megagroup else "channel", "access_hash": 0}

        async def _noop(*a, **k):
            return None

        monkeypatch.setattr(account_manager, "create_channel", fake_create)
        monkeypatch.setattr(session_simulator, "typing_delay", _noop)
        monkeypatch.setattr(session_simulator, "chaos_factor", lambda *a, **k: 0.0)
        monkeypatch.setattr(w.asyncio, "sleep", _noop)   # без пейсинг-задержек в тесте

        async def _cl():
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        async def _mk_op(params):
            return int(await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'bulk_create_channels','running',$2,0,'test') RETURNING id",
                OWNER, __import__("json").dumps(params)))

        try:
            await _cl()
            a1 = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name) "
                "VALUES($1,$2,$3,TRUE,'active','Acc1') RETURNING id", OWNER, "+79000000001", "s1"))
            a2 = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name) "
                "VALUES($1,$2,$3,TRUE,'active','Acc2') RETURNING id", OWNER, "+79000000002", "s2"))

            # 2 аккаунта × 2 канала = 4, нумерация, каналы.
            params = {"account_ids": [a1, a2], "title": "Test", "channel_count": 2,
                      "name_mode": "num", "is_group": False}
            op_id = await _mk_op(params)
            res = await w._exec_bulk_create_channels(pool, None, op_id, OWNER, params)
            assert res["status"] == "done" and res["created"] == 4, res
            assert len(calls) == 4
            assert {c[0] for c in calls} == {"Test 1", "Test 2", "Test 3", "Test 4"}
            assert all(mg is False for _, mg in calls)     # каналы, не группы
            cnt = await pool.fetchval("SELECT count(*) FROM managed_channels WHERE owner_id=$1", OWNER)
            assert int(cnt) == 4

            # Второй прогон: группы, одинаковое имя, по 1 на аккаунт.
            calls.clear()
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            params2 = {"account_ids": [a1, a2], "title": "G", "channel_count": 1,
                       "name_mode": "none", "is_group": True}
            op2 = await _mk_op(params2)
            res2 = await w._exec_bulk_create_channels(pool, None, op2, OWNER, params2)
            assert res2["created"] == 2, res2
            assert [c[0] for c in calls] == ["G", "G"]      # одинаковое имя
            assert all(mg is True for _, mg in calls)       # группы

            await _cl()
        finally:
            w._db_pool = _orig_pool      # восстановить прежний пул op_worker
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
