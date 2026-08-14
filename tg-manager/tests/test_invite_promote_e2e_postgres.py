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
        "join_channel": am.join_channel,
        "humanize": invite_behavior.humanize,
        "sleep": w.asyncio.sleep,
    }
    state = {"invite_calls": [], "promoted": [], "joined": [], "admin_id": None,
             "uid_to_id": {}}

    _sleep = asyncio.sleep

    async def fast(x):
        return await _sleep(0)

    async def fake_batch(session, acc, group, refs):
        aid = int(acc["id"])
        state["invite_calls"].append((aid, list(refs)))
        # no_connect: имитируем полный отказ подключения флота (сессия/сеть) —
        # батч не отработан (0 ok / 0 failed), ошибка «connect». attempted=0.
        if state.get("no_connect"):
            return {"ok": 0, "failed": 0, "peer_flood": False, "flood_wait": 0,
                    "errors": ["connect: сеть недоступна"], "privacy_failed": [],
                    "no_rights": False}
        # rights_only: у кого есть права; остальные → no_rights (проблема аккаунта).
        ro = state.get("rights_only")
        if ro is not None and aid not in ro:
            return {"ok": 0, "failed": 1, "peer_flood": False, "flood_wait": 0,
                    "errors": ["account error: нет прав"], "privacy_failed": [],
                    "no_rights": True}
        # Реализм: если вообще нет админа — прямой инвайт падает «нет прав» (group err).
        if state["admin_id"] == -1:
            return {"ok": 0, "failed": len(refs), "peer_flood": False, "flood_wait": 0,
                    "errors": ["group error: у аккаунта нет прав добавлять участников"],
                    "privacy_failed": [], "no_rights": False}
        return {"ok": len(refs), "failed": 0, "peer_flood": False,
                "flood_wait": 0, "errors": [], "privacy_failed": [], "no_rights": False}

    async def fake_admin_status(session, acc, group):
        # no_connect: проверка прав не выполняется (аккаунт не подключился).
        if state.get("no_connect"):
            return {"ok": False, "error": "connect: сеть недоступна"}
        return {"ok": True, "can_promote": int(acc["id"]) == state["admin_id"]}

    async def fake_promote(psession, group, uid, _acc=None, invite_users=False,
                           post_messages=False, add_admins=False):
        state["promoted"].append(int(uid))
        # РЕАЛИЗМ: выдача прав действительно даёт аккаунту права — добавляем его в
        # rights_only, чтобы следующий invite_batch от него прошёл (проверка того,
        # что промоутер выдаёт права безправным «на лету»).
        ro = state.get("rights_only")
        aid = state.get("uid_to_id", {}).get(int(uid))
        if ro is not None and aid is not None:
            ro.add(aid)
        return True

    async def fake_resolve(session, _acc=None):
        return 900000 + int(_acc["id"])

    async def fake_join(session, ref, _acc=None):
        state["joined"].append(int(_acc["id"]))
        return {"title": "T", "channel_id": 1}

    async def fake_humanize(acc, *a, **k):
        return None

    w.asyncio.sleep = fast
    inv.invite_batch = fake_batch
    inv.channel_admin_status = fake_admin_status
    am.promote_to_admin = fake_promote
    am.resolve_self_user_id = fake_resolve
    am.join_channel = fake_join
    invite_behavior.humanize = fake_humanize

    yield pool, w, state

    inv.invite_batch = orig["invite_batch"]
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


def _launch(pool, w, ids, run_id, n_users, auto_promote=True, **extra):
    async def _go():
        params = {"group": "@target", "source": "parsed", "parse_run_id": run_id,
                  "account_ids": ids, "auto_promote": auto_promote, "promote_trick": True}
        params.update(extra)
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
    state["joined"].clear()
    state["rights_only"] = None
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
    # КЛЮЧЕВОЕ (баг «админка не выдавалась»): инвайтеры-не-промоутеры ВСТУПИЛИ в
    # чат ДО выдачи прав (иначе promote_to_admin падает UserNotParticipant).
    assert ids[1] in state["joined"] and ids[2] in state["joined"]


def test_no_admin_surfaces_clear_reason(env):
    pool, w, state = env
    state["invite_calls"].clear()
    state["promoted"].clear()
    state["rights_only"] = None
    ids, run_id = _seed(pool, n_acc=2, n_users=5)
    state["admin_id"] = -1   # НИ ОДИН аккаунт не админ → промоутеру неоткуда взяться
    row, _ = _launch(pool, w, ids, run_id, 5)
    # промоут не выдан; инвайт не прошёл (нет прав), и summary честно называет причину
    assert state["promoted"] == []
    assert row["done_items"] == 0 or "0/" in (row["summary"] or "")
    assert "админ" in (row["summary"] or "").lower()


def test_promoter_grants_rights_on_demand_whole_fleet_works(env):
    """Ключевое: аккаунт С правами (промоутер) ВЫДАЁТ права безправным на лету,
    чтобы работал ВЕСЬ флот, а не только создатель. Стартуем с правами только у
    создателя; промоутер выдаёт права ids[1],ids[2] по ходу → они тоже инвайтят,
    и операция не валится."""
    pool, w, state = env
    state["invite_calls"].clear()
    state["promoted"].clear()
    state["joined"].clear()
    ids, run_id = _seed(pool, n_acc=3, n_users=12)
    state["uid_to_id"] = {800000 + i: ids[i] for i in range(len(ids))}
    state["admin_id"] = ids[0]        # создатель = промоутер
    state["rights_only"] = {ids[0]}   # изначально права ТОЛЬКО у создателя
    # auto_promote=False → пре-цикловой автовыдачи нет; проверяем именно ON-DEMAND
    row, _ = _launch(pool, w, ids, run_id, 12, auto_promote=False)
    assert row["status"] == "done", row["summary"]
    assert row["done_items"] == 12
    # безправным реально выдали права на лету (их uid промоутнуты)
    assert {800001, 800002} & set(state["promoted"])
    # и в итоге в rights_only попали все — весь флот получил права
    assert ids[1] in state["rights_only"] and ids[2] in state["rights_only"]
    # инвайтили не только создатель, но и дополученные права аккаунты
    workers = {aid for aid, refs in state["invite_calls"] if aid in (ids[1], ids[2])}
    assert workers, "аккаунты, дополучившие права, должны были участвовать в инвайте"
    state["rights_only"] = None


def test_per_account_volume_one_pass(env):
    """Жалоба «берётся 2 юзера на аккаунт»: с явным объёмом (one_pass + лимит N)
    каждый аккаунт делает до N инвайтов, а не консервативные ~2 по истории."""
    pool, w, state = env
    state["invite_calls"].clear()
    state["promoted"].clear()
    state["rights_only"] = None
    ids, run_id = _seed(pool, n_acc=3, n_users=30)
    state["admin_id"] = ids[0]           # админ есть → всё подключается и инвайтит
    # объём 4 на аккаунт, один проход (без клампа предсказанным дневным лимитом)
    row, _ = _launch(pool, w, ids, run_id, 30, per_account_limit=4, one_pass=True)
    assert row["status"] == "done", row["summary"]
    # каждый аккаунт добавил РОВНО по 4 (3×4=12), а не по 2
    per_acc = {}
    for aid, refs in state["invite_calls"]:
        per_acc[aid] = per_acc.get(aid, 0) + len(refs)
    assert row["done_items"] == 12, (row["done_items"], per_acc)
    assert all(v <= 4 for v in per_acc.values()), per_acc
    assert set(per_acc.values()) == {4}, per_acc   # у всех троих по 4


def test_no_connect_does_not_claim_missing_admin(env):
    """Жалоба «но ведь у одного аккаунта были права админа»: если весь флот не
    подключился, проверить права нельзя — и бот НЕ должен утверждать «админа нет».
    Раньше при полном отказе подключения показывалось ложное «ни один аккаунт не
    админ», хотя админ был — просто не подключился."""
    pool, w, state = env
    state["invite_calls"].clear()
    state["promoted"].clear()
    state["rights_only"] = None
    ids, run_id = _seed(pool, n_acc=3, n_users=8)
    state["admin_id"] = ids[0]      # админ ЕСТЬ (но подключиться никто не смог)
    state["no_connect"] = True      # весь флот не подключается (сессия/сеть)
    try:
        row, _ = _launch(pool, w, ids, run_id, 8)
    finally:
        state["no_connect"] = False
    _sum = (row["summary"] or "").lower()
    # никого не добавили
    assert row["done_items"] == 0
    # КЛЮЧЕВОЕ: НЕ утверждаем «ни один ваш аккаунт не админ» (это было бы ложью —
    # админ был, но флот не подключился). Причина должна указывать на сессию/сеть.
    assert "ни один ваш аккаунт не админ" not in _sum
    assert "промоут" not in _sum or "не выполнена" in _sum or "не подключил" in _sum
