"""«Клон и адаптация» на живом Postgres: исполнитель, история, откат.

Заглушка пула не проверяет типы и не знает новых колонок истории (op_id,
changes, settings из schema_v317), поэтому цепочка проверяется на настоящей
базе: исполнитель пишет снимок и историю, ручка истории их показывает, откат
ставится операцией своего типа и только по своему клону.

Telegram подменён: движок читает «профили» из словарей и записывает каждый
вызов. Токен бота в базе лежит зашифрованным — как в проде, — и исполнитель
обязан отдать в Telegram расшифрованный (до исправления уходил шифротекст).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 995301
STRANGER = 995302
SRC, T1, T2 = 99530101, 99530102, 99530103
TOKENS = {SRC: "111:src", T1: "222:t1", T2: "333:t2"}
PROFILES = {
    "111:src": {"name": "Доставка Москва", "desc": "Пишите @src_bot", "user": "src_bot"},
    "222:t1": {"name": "Старое", "desc": "старое описание", "user": "t1_bot"},
    "333:t2": {"name": "Доставка Казань", "desc": "Пишите @t2_bot", "user": "t2_bot"},
}


def _go(body):
    async def run():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import clone_adapt_engine as ca
        from services import mini_app_api as M
        from services import operation_bus as B
        from services.token_vault import encrypt_token

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        calls: list[tuple[str, str, dict]] = []

        async def fake_read(sess, token, fields, langs, *, username=""):
            p = PROFILES[token]
            return {"username": p["user"],
                    "langs": {"": {"name": p["name"], "description": p["desc"],
                                   "short_description": ""}},
                    "commands": {"": []}}

        async def fake_photo(sess, token):
            return None

        async def fake_apply(sess, token, step, photo=None):
            calls.append((token, step["key"], step["after"]))
            return {"ok": True}

        orig = (ca.read_profile, ca.read_photo, ca.apply_step, M._get_uid, B._enforce_min_plan)

        async def _no_gate(*a, **k):
            return False

        ca.read_profile, ca.read_photo, ca.apply_step = fake_read, fake_photo, fake_apply
        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))
        B._enforce_min_plan = _no_gate
        owners = [OWNER, STRANGER]
        try:
            for t in ("clone_adapt_history", "bulk_edit_snapshots", "operation_queue"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                               list(TOKENS))
            for bid, tok in TOKENS.items():
                await pool.execute(
                    "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
                    "VALUES($1,$2,$3,TRUE,$4)", encrypt_token(tok), bid,
                    PROFILES[tok]["user"], OWNER)
            stored = await pool.fetchval("SELECT token FROM managed_bots WHERE bot_id=$1", T1)
            assert stored.startswith("ENC:"), "токен в базе обязан быть зашифрован, как в проде"
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool, calls)
            finally:
                await client.close()
        finally:
            (ca.read_profile, ca.read_photo, ca.apply_step, M._get_uid,
             B._enforce_min_plan) = orig
            try:
                for t in ("clone_adapt_history", "bulk_edit_snapshots", "operation_queue"):
                    await pool.execute(f"DELETE FROM {t} WHERE owner_id = ANY($1::bigint[])", owners)
                await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                                   list(TOKENS))
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


async def _submit_and_run(client, pool):
    from services import op_worker

    r = await client.post("/api/miniapp/clone_adapt/submit", json={
        "source_bot_id": SRC, "target_bot_ids": [T1, T2],
        "fields": ["name", "desc"],
        "settings": {"replacements": [["Москва", "Казань"]]},
    })
    assert r.status == 200, await r.text()
    op_id = (await r.json())["op_id"]
    params = await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", op_id)
    params = json.loads(params) if isinstance(params, str) else params
    assert params["target_bot_ids"] == [T1, T2]
    res = await op_worker._exec_clone_adapt(pool, None, op_id, OWNER, params)
    return op_id, res


def test_clone_runs_with_decrypted_tokens_and_skips_what_already_matches():
    async def body(client, pool, calls):
        op_id, res = await _submit_and_run(client, pool)
        assert res["status"] == "done", res
        # Токены — расшифрованные: в Telegram уходит «222:t1», а не ENC:….
        assert {c[0] for c in calls} == {"222:t1"}, calls
        # У второго бота после адаптации всё уже совпадает — Telegram не трогали.
        assert ("222:t1", "name", "Доставка Казань") in calls
        assert ("222:t1", "description", "Пишите @t1_bot") in calls
        hist = {r["target_bot_id"]: r for r in await pool.fetch(
            "SELECT * FROM clone_adapt_history WHERE owner_id=$1", OWNER)}
        assert hist[T1]["status"] == "ok" and hist[T2]["status"] == "same"
        assert hist[T1]["op_id"] == op_id
        ch = json.loads(hist[T1]["changes"]) if isinstance(hist[T1]["changes"], str) else hist[T1]["changes"]
        assert {c["label"] for c in ch} == {"Имя", "Описание"}
        snap = await pool.fetchval(
            "SELECT before FROM bulk_edit_snapshots WHERE owner_id=$1 AND op_id=$2 AND target_id=$3",
            OWNER, op_id, T1)
        snap = json.loads(snap) if isinstance(snap, str) else snap
        assert snap == {"name": "Старое", "description": "старое описание"}
        # Повтор операции не трогает уже сделанных.
        calls.clear()
        params = await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", op_id)
        params = json.loads(params) if isinstance(params, str) else params
        from services import op_worker
        await op_worker._exec_clone_adapt(pool, None, op_id, OWNER, params)
        assert calls == []
    _go(body)


def test_history_shows_details_and_rollback_is_scoped_to_the_owner():
    async def body(client, pool, calls):
        op_id, _ = await _submit_and_run(client, pool)
        r = await client.get("/api/miniapp/clone_adapt/history?limit=30")
        assert r.status == 200, await r.text()
        d = await r.json()
        assert d["total"] == 2 and d["stats"]["runs"] == 1
        row = next(i for i in d["items"] if i["target_bot_id"] == T1)
        assert row["can_rollback"] is True and row["fields_label"] == "имя, описание"
        assert row["settings"]["replacements"] == [["Москва", "Казань"]]
        r = await client.get("/api/miniapp/clone_adapt/history?status=problem")
        assert (await r.json())["total"] == 0

        r = await client.post("/api/miniapp/clone_adapt/rollback", json={"op_id": op_id},
                              headers={"X-Uid": str(STRANGER)})
        assert r.status == 400, "чужой клон откатывать нельзя"
        r = await client.post("/api/miniapp/clone_adapt/rollback", json={"op_id": op_id})
        assert r.status == 200, await r.text()
        rb = await r.json()
        assert rb["count"] == 1
        assert await pool.fetchval("SELECT op_type FROM operation_queue WHERE id=$1",
                                   rb["op_id"]) == "clone_adapt_rollback"
    _go(body)
