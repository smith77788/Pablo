"""Инвайт «ссылка в ЛС» как метод в op_worker (живой Postgres).

При invite_method='link' op_worker должен: (1) один раз экспортировать
ссылку-приглашение, (2) рассылать её флотом через invite_via_link_batch, НЕ трогая
права админа (промоутер не ищется). Заглушены только seam'ы Telegram.
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

OWNER = 995303
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
        "export": inv.export_group_invite_link,
        "link_batch": inv.invite_via_link_batch,
        "channel_admin_status": inv.channel_admin_status,
        "promote_to_admin": am.promote_to_admin,
        "humanize": invite_behavior.humanize,
        "sleep": w.asyncio.sleep,
    }
    state = {"exported": 0, "link_calls": [], "promote_called": False,
             "admin_checked": False}
    _sleep = asyncio.sleep

    async def fast(x):
        return await _sleep(0)

    async def fake_export(session, acc, group):
        state["exported"] += 1
        return "https://t.me/+SECRET"

    # Сигнатура повторяет живую invite_via_link_batch (session, acc, link, refs,
    # message_text, pace_mult) и терпима к добавлению параметров: иначе
    # каждый новый аргумент движка молча ломает весь этот файл, а увидеть
    # это можно только с живым Postgres. Так и случилось с pace_mult.
    async def fake_link_batch(session, acc, link, refs, msg=None,
                              pace_mult=1.0, *args, **kwargs):
        state["link_calls"].append((int(acc["id"]), link, list(refs)))
        return {"ok": len(refs), "failed": 0, "peer_flood": False,
                "flood_wait": 0, "errors": [], "privacy_failed": [], "no_rights": False}

    async def fake_admin_status(session, acc, group):
        state["admin_checked"] = True
        return {"ok": True, "can_promote": False}

    async def fake_promote(*a, **k):
        state["promote_called"] = True
        return True

    async def fake_humanize(acc, *a, **k):
        return None

    w.asyncio.sleep = fast
    inv.export_group_invite_link = fake_export
    inv.invite_via_link_batch = fake_link_batch
    inv.channel_admin_status = fake_admin_status
    am.promote_to_admin = fake_promote
    invite_behavior.humanize = fake_humanize

    yield pool, w, state

    inv.export_group_invite_link = orig["export"]
    inv.invite_via_link_batch = orig["link_batch"]
    inv.channel_admin_status = orig["channel_admin_status"]
    am.promote_to_admin = orig["promote_to_admin"]
    invite_behavior.humanize = orig["humanize"]
    w.asyncio.sleep = orig["sleep"]
    _run(pool.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _seed(pool, *, n_acc=3, n_users=12):
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
                  "account_ids": ids, "invite_method": "link"}
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


def test_link_method_broadcasts_and_skips_admin(env):
    pool, w, state = env
    state["exported"] = 0
    state["link_calls"].clear()
    state["promote_called"] = False
    state["admin_checked"] = False
    ids, run_id = _seed(pool, n_acc=3, n_users=12)
    row, _ = _launch(pool, w, ids, run_id, 12)
    assert row["status"] == "done", row["summary"]
    # вся аудитория получила ссылку
    assert row["done_items"] == 12
    _sent = sum(len(c[2]) for c in state["link_calls"])
    assert _sent == 12
    # ссылка экспортирована (хотя бы раз) и это именно она пошла в рассылку
    assert state["exported"] >= 1
    assert all(c[1] == "https://t.me/+SECRET" for c in state["link_calls"])
    # метод «link» НЕ трогает права: промоутер не ищется, promote не зовётся
    assert state["promote_called"] is False
    assert state["admin_checked"] is False
