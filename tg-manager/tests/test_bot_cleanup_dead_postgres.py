"""Очистка Mini App на PostgreSQL с имитацией ответов Telegram.

Аудитория остаётся после пакетного и автоматического скрытия. Автоматическая
очистка сравнивает сохранённый токен с тем, который проверяла.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 7770001
OTHER = 8880002


async def _load_schema(conn) -> None:
    for f in ["schema.sql"] + sorted(
        glob.glob("schema_v*.sql"),
        key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)),
    ):
        try:
            await conn.execute(open(f, encoding="utf-8").read())
        except Exception:
            pass


def test_check_bulk_delete_and_cleanup_dead(monkeypatch):
    import asyncio

    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import bot_api
    from services import mini_app_api
    from services import mini_app_bots

    # uid из заголовка X-Uid — чтобы не подписывать настоящий токен в тесте.
    # Патчим ДО setup_routes: замыкания берут _get_uid из модуля в момент сборки.
    monkeypatch.setattr(mini_app_api, "_get_uid",
                        lambda request: int(request.headers["X-Uid"])
                        if request.headers.get("X-Uid") else None)

    # record_manual_action уводим в no-op: аудит не предмет этого теста, а его
    # таблица не нужна для проверки удаления.
    from database import db as _db

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(_db, "record_manual_action", _noop)

    # Статус живости — по тексту токена, без сети. unknown = «Telegram не
    # ответил», и он не должен удаляться.
    def _status_for(token: str) -> str:
        if "dead" in token:
            return bot_api.BOT_DEAD
        if "unknown" in token:
            return bot_api.BOT_UNKNOWN
        return bot_api.BOT_ALIVE

    rotate = None

    async def _fake_batch(session, tokens):
        if rotate is not None:
            await rotate()
        return [_status_for(t) for t in tokens]
    monkeypatch.setattr(bot_api, "batch_check_alive", _fake_batch)

    async def go():
        nonlocal rotate
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute(
                "DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])",
                [OWNER, OTHER])
            # У владельца живой бот, два с отозванным токеном, один без
            # ответа и один на паузе из отчёта. Плюс чужой бот.
            bots = [
                (101, "t_alive_101", OWNER),
                (102, "t_dead_102", OWNER),
                (103, "t_unknown_103", OWNER),
                (104, "t_dead_104", OWNER),
                (105, "t_dead_105", OWNER),
                (201, "t_alive_201", OTHER),
            ]
            for bid, tok, owner in bots:
                await pool.execute(
                    "INSERT INTO managed_bots(bot_id, token, username, is_active, added_by) "
                    "VALUES($1,$2,$3,TRUE,$4)", bid, tok, f"bot{bid}", owner)

            await pool.execute("UPDATE managed_bots SET is_active=FALSE, cleanup_paused=TRUE WHERE bot_id=105")
            await pool.execute(
                "INSERT INTO bot_users(bot_id,user_id) VALUES(101,501),(102,502),(104,504),(105,505)")

            app = web.Application()
            mini_app_bots.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                hdr = {"X-Uid": str(OWNER)}

                # 1) check_all — только чтение, ничего не удаляет.
                r = await client.post("/api/miniapp/bots/check_all", headers=hdr, json={})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["alive"] == 1 and d["dead"] == 3 and d["unknown"] == 1, d
                by_id = {b["bot_id"]: b["status"] for b in d["bots"]}
                assert by_id[102] == "dead" and by_id[103] == "unknown", by_id
                # Ничего не удалено проверкой.
                n = await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", OWNER)
                assert n == 5, "check_all не должен удалять"

                # Ротация между проверкой и записью должна отменить снятие бота.
                async def rotate_token():
                    assert await _db.replace_bot_token(pool, OWNER, 104, "t_alive_rotated_104")

                rotate = rotate_token
                r = await client.post("/api/miniapp/bots/cleanup_dead", headers=hdr, json={})
                rotate = None
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["checked"] == 4 and d["deleted"] == 1 and d["dead"] == 2, d
                assert d["unknown"] == 1 and d["alive"] == 1 and d["changed_since_check"] == 1
                assert d["deleted_ids"] == [102]
                assert "t_dead" not in str(d) and "t_alive" not in str(d)
                state = {row["bot_id"]: (row["is_active"], row["deleted_at"] is not None,
                                        row["cleanup_paused"]) for row in await pool.fetch(
                    "SELECT bot_id,is_active,deleted_at,cleanup_paused "
                    "FROM managed_bots WHERE added_by=ANY($1::bigint[])", [OWNER, OTHER])}
                assert state == {101: (True, False, False), 102: (False, True, False),
                                 103: (True, False, False), 104: (True, False, False),
                                 105: (False, False, True), 201: (True, False, False)}
                assert not await _db.add_bot(pool, "t_dead_105", 105, "bot105", "Paused", OWNER)
                assert await pool.fetchval("SELECT count(*) FROM bot_users WHERE bot_id=102") == 1

                r = await client.post("/api/miniapp/bots/bulk_delete", headers=hdr,
                                      json={"bot_ids": [101, 201]})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["requested"] == 2 and d["deleted"] == 1, d
                assert await pool.fetchval(
                    "SELECT deleted_at IS NOT NULL AND NOT is_active FROM managed_bots WHERE bot_id=101")
                assert await pool.fetchval(
                    "SELECT deleted_at IS NULL AND is_active FROM managed_bots WHERE bot_id=201")
                assert await pool.fetchval("SELECT count(*) FROM bot_users WHERE bot_id=101") == 1

                # 4) Идемпотентность: повтор по уже удалённому = 0, не ошибка.
                r = await client.post("/api/miniapp/bots/bulk_delete", headers=hdr,
                                      json={"bot_ids": [101]})
                assert r.status == 200
                assert (await r.json())["deleted"] == 0

                for malformed in ([], {"bot_ids": []}, {"bot_ids": [True]},
                                  {"bot_ids": [101.9]}, {"bot_ids": [2**63]}):
                    r = await client.post("/api/miniapp/bots/bulk_delete", headers=hdr, json=malformed)
                    assert r.status == 400

                assert await _db.add_bot(pool, "t_alive_101", 101, "bot101", "Returned", OWNER)
                assert await pool.fetchval("SELECT count(*) FROM bot_users WHERE bot_id=101") == 1
                assert await pool.fetchval(
                    "SELECT deleted_at IS NULL AND is_active FROM managed_bots WHERE bot_id=101")
            finally:
                await client.close()

            await pool.execute(
                "DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])",
                [OWNER, OTHER])
        finally:
            await pool.close()

    asyncio.run(go())
