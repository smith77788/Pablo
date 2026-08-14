"""Сквозной инвайт с автовыдачей админки (промоут-путь) по живому Postgres.

Симптом «не назначаются админы» — проверяем, что при наличии аккаунта-админа с
правом промоута остальным инвайтерам РЕАЛЬНО выдаётся админка, и инвайт идёт.
Заглушены только seam'ы Telegram (invite_batch/channel_admin_status/promote_to_admin).
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

OWNER = 995101
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
        "channel_admin_status": inv.channel_admin_status,
        "promote_to_admin": am.promote_to_admin,
        "resolve_self_user_id": am.resolve_self_user_id,
        "humanize": invite_behavior.humanize,
        "sleep": w.asyncio.sleep,
    }
    state = {"invite_calls": [], "promoted": [], "admin_id": None}

    _sleep = asyncio.sleep

    async def fast(x):
        return await _sleep(0)

    async def fake_batch(session, acc, group, refs):
        state["invite_calls"].append((int(acc["id"]), list(refs)))
        # Реализм: без админа прямой инвайт в канал падает «нет прав» (group error).
        if state["admin_id"] == -1:
            return {"ok": 0, "failed": len(refs), "peer_flood": False, "flood_wait": 0,
                    "errors": ["group error: у аккаунта нет прав добавлять участников"],
                    "privacy_failed": []}
        return {"ok": len(refs), "failed": 0, "peer_flood": False,
                "flood_wait": 0, "errors": [], "privacy_failed": []}

    async def fake_admin_status(session, acc, group):
        return {"ok": True, "can_promote": int(acc["id"]) == state["admin_id"]}

    async def fake_promote(psession, group, uid, _acc=None, invite_users=False, post_messages=False):
        state["promoted"].append(int(uid))
        return True

    async def fake_resolve(session, _acc=None):
        return 900000 + int(_acc["id"])

    async def fake_humanize(acc, *a, **k):
        return None

    w.asyncio.sleep = fast
    inv.invite_batch = fake_batch
    inv.channel_admin_status = fake_admin_status
    am.promote_to_admin = fake_promote
    am.resolve_self_user_id = fake_resolve
    invite_behavior.humanize = fake_humanize

    yield pool, w, state

    inv.invite_batch = orig["invite_batch"]
    inv.channel_admin_status = orig["channel_admin_status"]
    am.promote_to_admin = orig["promote_to_admin"]
    am.resolve_self_user_id = orig["resolve_self_user_id"]
    invite_behavior.humanize = orig["humanize"]
    w.asyncio.sleep = orig["sleep"]
    _run(pool.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _seed(pool, *, n_acc=3, n_users=20, with_uid=True):
    async def _s():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM parser_runs WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        # дедуп инвайта персистентный — чистим, иначе цели из прошлого теста
        # отсекаются как «уже приглашались».
        for _t in ("invite_target_log",):
            try:
                await pool.execute(f"DELETE FROM {_t} WHERE owner_id=$1", OWNER)
            except Exception:
                pass
        ids = []
        for i in range(n_acc):
            uid = (800000 + i) if with_uid else None
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "first_name,tg_user_id) VALUES($1,$2,$3,TRUE,'active',$4,$5) RETURNING id",
                OWNER, f"+7995{OWNER%100}{i:05d}", f"s{i}", f"A{i}", uid))
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
                  "account_ids": ids, "auto_promote": True, "promote_trick": True}
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


def test_admins_assigned_and_audience_invited(env):
    pool, w, state = env
    state["invite_calls"].clear()
    state["promoted"].clear()
    ids, run_id = _seed(pool, n_acc=3, n_users=20)
    state["admin_id"] = ids[0]   # первый аккаунт — админ с правом промоута
    row, _ = _launch(pool, w, ids, run_id, 20)
    assert row["status"] == "done", row["summary"]
    # вся аудитория приглашена
    assert row["done_items"] == 20
    assert sum(len(c[1]) for c in state["invite_calls"]) == 20
    # админка РЕАЛЬНО выдана остальным инвайтерам (их tg_user_id = 800001, 800002)
    assert {800001, 800002}.issubset(set(state["promoted"]))
    assert "Выдана админка" in (row["summary"] or "")


def test_no_admin_surfaces_clear_reason(env):
    pool, w, state = env
    state["invite_calls"].clear()
    state["promoted"].clear()
    ids, run_id = _seed(pool, n_acc=2, n_users=5)
    state["admin_id"] = -1   # НИ ОДИН аккаунт не админ → промоутеру неоткуда взяться
    row, _ = _launch(pool, w, ids, run_id, 5)
    # промоут не выдан; инвайт не прошёл (нет прав), и summary честно называет причину
    assert state["promoted"] == []
    assert row["done_items"] == 0 or "0/" in (row["summary"] or "")
    assert "админ" in (row["summary"] or "").lower()
