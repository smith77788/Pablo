"""Длинный (суточный) FloodWait останавливает инвайт РАНЬШЕ — не выжигает весь флот.

Отдельный файл со своим пулом и стабами: перегревный прогон на 6 аккаунтах иначе
пачкал бы общий e2e-стенд (модульный пул, общий loop, in-memory состояние воркера).

Реальный лог владельца: 7 аккаунтов получили 86400с (суточный) FloodWait, а
операция всё шла и жгла флот дальше. Такой флуд — это бан аккаунта на приглашения
на сутки, а не «притормози»; несколько подряд = весь флот у потолка по этому чату.
Порог INVITE_LONG_FLOOD_STOP=3: из 6 аккаунтов в работу уйдут ~3, а не 5 (как со
старым общим стоп-краном на 5 флудов любого размера).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 991777
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def test_long_floodwait_stops_before_burning_whole_fleet():
    import asyncpg
    from services import mass_inviter_engine as inv, invite_behavior, op_worker as w

    async def go():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)

        per_acc: dict = {}

        async def _engine(session, acc, group, refs, *a, **k):
            per_acc.setdefault(int(acc["id"]), []).extend(refs)
            # Суточный флуд у КАЖДОГО аккаунта.
            return {"ok": 0, "failed": 1, "flood_wait": 90000,
                    "errors": ["flood wait 90000s"]}

        async def _noop(*a, **k):
            return None

        async def _fast(x):
            return await asyncio.sleep(0)

        _orig = (inv.invite_batch, inv.invite_by_phones,
                 invite_behavior.humanize, w.asyncio.sleep, w._db_pool)
        inv.invite_batch = _engine
        inv.invite_by_phones = _engine
        invite_behavior.humanize = _noop
        w.asyncio.sleep = _fast
        w.init_op_worker_pool(pool)
        try:
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            for i in range(6):
                await pool.execute(
                    "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                    "first_name) VALUES($1,$2,$3,TRUE,'active',$4)",
                    OWNER, f"+7991{i:07d}", f"s{i}", f"A{i}")

            refs = [f"@lf{i}" for i in range(30)]
            params = {"group": "@g", "source": "import_list", "user_refs": refs}
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'mass_invite','running',$2,$3,'lf') RETURNING id",
                OWNER, json.dumps(params), 30)
            row = await pool.fetchrow(
                "SELECT id, owner_id, op_type, params FROM operation_queue WHERE id=$1", op_id)
            await w._run_op_task(pool, None, dict(row))

            burned = len(per_acc)   # сколько РАЗНЫХ аккаунтов реально пробовали слать
            assert burned <= 4, f"флот выжжен в суточные баны: {burned} аккаунтов (ждём ≤4)"

            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        finally:
            (inv.invite_batch, inv.invite_by_phones,
             invite_behavior.humanize, w.asyncio.sleep, w._db_pool) = _orig
            async with w._accounts_lock:
                w._accounts_in_use.clear()
                w._operation_account_locks.clear()
            async with w._active_lock:
                w._active_op_ids.clear()
            await pool.close()

    _run(go())
