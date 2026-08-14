"""Инвайт «через админку» (промоут-трюк как основной метод) по живому Postgres.

Сценарий пользователя: выбрал пользователя из базы → сделал его админом в
чате/канале (это добавляет его в чат) → снял права → пользователь остался
участником. Проверяем, что при invite_method='admin':
  • op_worker зовёт add_via_promote (а НЕ прямой invite_batch);
  • вся аудитория добавлена;
  • флоту выдаётся add_admins (право промоута), чтобы трюк работал не только с
    аккаунта-создателя, а со всего флота.
Заглушены только seam'ы Telegram.
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

OWNER = 995202
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def env():
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
        pool = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")

    from services import (op_worker as w, mass_inviter_engine as inv,
                          account_manager as am, invite_behavior)
    orig = {
        "invite_batch": inv.invite_batch,
        "add_via_promote": inv.add_via_promote,
        "channel_admin_status": inv.channel_admin_status,
        "promote_to_admin": am.promote_to_admin,
        "resolve_self_user_id": am.resolve_self_user_id,
        "join_channel": am.join_channel,
        "humanize": invite_behavior.humanize,
        "sleep": w.asyncio.sleep,
    }
    state = {"promote_calls": [], "add_admins_grants": [], "batch_calls": [],
             "admin_id": None}

    _sleep = asyncio.sleep

    async def fast(x):
        return await _sleep(0)

    async def fake_add_via_promote(session, acc, group, refs):
        state["promote_calls"].append((int(acc["id"]), list(refs)))
        return {"ok": len(refs), "failed": 0, "peer_flood": False,
                "flood_wait": 0, "errors": [], "no_rights": False}

    async def fake_batch(session, acc, group, refs):
        state["batch_calls"].append((int(acc["id"]), list(refs)))
        return {"ok": len(refs), "failed": 0, "peer_flood": False,
                "flood_wait": 0, "errors": [], "privacy_failed": [], "no_rights": False}

    async def fake_admin_status(session, acc, group):
        return {"ok": True, "can_promote": int(acc["id"]) == state["admin_id"]}

    async def fake_promote(psession, group, uid, _acc=None, invite_users=False,
                           post_messages=False, add_admins=False):
        state["promote_calls"].append(("grant", int(uid), bool(add_admins)))
        if add_admins:
            state["add_admins_grants"].append(int(uid))
        return True

    async def fake_resolve(session, _acc=None):
        return 900000 + int(_acc["id"])

    async def fake_join(session, ref, _acc=None):
        return {"title": "T", "channel_id": 1}

    async def fake_humanize(acc, *a, **k):
        return None

    w.asyncio.sleep = fast
    inv.invite_batch = fake_batch
    inv.add_via_promote = fake_add_via_promote
    inv.channel_admin_status = fake_admin_status
    am.promote_to_admin = fake_promote
    am.resolve_self_user_id = fake_resolve
    am.join_channel = fake_join
    invite_behavior.humanize = fake_humanize

    yield pool, w, state

    inv.invite_batch = orig["invite_batch"]
    inv.add_via_promote = orig["add_via_promote"]
    inv.channel_admin_status = orig["channel_admin_status"]
    am.promote_to_admin = orig["promote_to_admin"]
    am.resolve_self_user_id = orig["resolve_self_user_id"]
    am.join_channel = orig["join_channel"]
    invite_behavior.humanize = orig["humanize"]
    w.asyncio.sleep = orig["sleep"]
    _run(pool.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _seed(pool, *, n_acc=3, n_users=15):
    async def _s():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM parser_runs WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        for _t in ("invite_target_log",):
            try:
                await pool.execute(f"DELETE FROM {_t} WHERE owner_id=$1", OWNER)
            except Exception:
                pass
        ids = []
        for i in range(n_acc):
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "first_name,tg_user_id) VALUES($1,$2,$3,TRUE,'active',$4,$5) RETURNING id",
                OWNER, f"+7995{OWNER%100}{i:05d}", f"s{i}", f"A{i}", 800000 + i))
        run_id = await pool.fetchval(
            "INSERT INTO parser_runs(owner_id,source_type,source_ref,parse_type,status) "
            "VALUES($1,'channel','X','members','done') RETURNING id", OWNER)
        for u in range(n_users):
            await pool.execute(
                "INSERT INTO parsed_audiences(owner_id,source_type,source_id,parse_run_id,"
                "tg_user_id,username) VALUES($1,'channel',$2,$2,$3,$4)",
                OWNER, run_id, 500000 + u, f"u{u}")
        return ids, run_id
    return _run(_s())


def _launch(pool, w, ids, run_id, n_users):
    async def _go():
        params = {"group": "@target", "source": "parsed", "parse_run_id": run_id,
                  "account_ids": ids, "auto_promote": True, "promote_trick": True,
                  "invite_method": "admin"}
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,'mass_invite','pending',$2,$3,'e2e') RETURNING id",
            OWNER, json.dumps(params), n_users)
        rows = await pool.fetch(
            "UPDATE operation_queue SET status='running',started_at=now() WHERE id=$1 "
            "RETURNING id,owner_id,op_type,params", op_id)
        await w._run_op_task(pool, None, dict(rows[0]))
        return await pool.fetchrow(
            "SELECT status, done_items, result->>'summary' AS summary FROM operation_queue "
            "WHERE id=$1", op_id), op_id
    return _run(_go())


def test_admin_method_uses_promote_trick_and_invites_all(env):
    pool, w, state = env
    state["promote_calls"].clear()
    state["add_admins_grants"].clear()
    state["batch_calls"].clear()
    ids, run_id = _seed(pool, n_acc=3, n_users=15)
    state["admin_id"] = ids[0]   # создатель — промоутер с add_admins
    row, _ = _launch(pool, w, ids, run_id, 15)
    assert row["status"] == "done", row["summary"]
    # вся аудитория добавлена промоут-трюком
    assert row["done_items"] == 15
    # цели шли через add_via_promote, а НЕ через прямой invite_batch
    _added = sum(len(c[1]) for c in state["promote_calls"]
                 if isinstance(c[0], int) and c[0] in ids)
    assert _added == 15, state["promote_calls"]
    assert state["batch_calls"] == [], "при методе admin прямой invite_batch не зовём"
    # флоту выдан add_admins (право промоута), а не только invite_users —
    # иначе не-создатели не смогли бы делать промоут-трюк
    assert state["add_admins_grants"], "флоту должен выдаваться add_admins"
