"""Единое обслуживание флота «сделать всё разом»: параллельно и без лишнего.

Один вызов POST /api/miniapp/fleet/sweep проходит по ботам, аккаунтам и
каналам сразу и возвращает общую сводку. Проверяем на живом Postgres:
  • режим проверки (delete=false) ничего не удаляет;
  • preview (delete=true, preview=true) считает, но не удаляет;
  • удаление сносит ТОЛЬКО мёртвое — бота с отозванным токеном и аккаунт с
    терминальным статусом; бот «не ответил» (unknown), живой бот, рабочий
    аккаунт и каналы остаются; чужое не трогается.

Живой Postgres обязателен: заглушка пула не проверит ни связывание
ANY($1::bigint[]), ни owner-скоуп параллельных DELETE.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 7770005
OTHER = 8880006


async def _load_schema(conn) -> None:
    for f in ["schema.sql"] + sorted(
        glob.glob("schema_v*.sql"),
        key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)),
    ):
        try:
            await conn.execute(open(f, encoding="utf-8").read())
        except Exception:
            pass


def test_sweep_checks_and_cleans_all_types(monkeypatch):
    import asyncio

    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import bot_api
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid",
                        lambda request: int(request.headers["X-Uid"])
                        if request.headers.get("X-Uid") else None)
    from database import db as _db

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(_db, "record_manual_action", _noop)

    def _status_for(token: str) -> str:
        if "dead" in token:
            return bot_api.BOT_DEAD
        if "unknown" in token:
            return bot_api.BOT_UNKNOWN
        return bot_api.BOT_ALIVE

    async def _fake_batch(session, tokens):
        return [_status_for(t) for t in tokens]
    monkeypatch.setattr(bot_api, "batch_check_alive", _fake_batch)

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])", [OWNER, OTHER])
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])", [OWNER, OTHER])
            await pool.execute("DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])", [OWNER, OTHER])

            # Боты владельца: жив, мёртв, не ответил. Плюс чужой мёртвый.
            for bid, tok, owner in [
                (301, "t_alive_301", OWNER), (302, "t_dead_302", OWNER),
                (303, "t_unknown_303", OWNER), (401, "t_dead_401", OTHER)]:
                await pool.execute(
                    "INSERT INTO managed_bots(bot_id, token, username, is_active, added_by) "
                    "VALUES($1,$2,$3,TRUE,$4)", bid, tok, f"b{bid}", owner)
            # Аккаунты владельца: рабочий, забанен и деактивирован (LOST —
            # удалять), кулдаун и session_expired (восстановимо — НЕ удалять).
            # Плюс чужой забаненный. Проверяем, что session_expired переживает
            # очистку: сессия переподключается, выбрасывать его нельзя.
            for aid, status, owner in [
                (501, "active", OWNER), (502, "banned", OWNER),
                (503, "deactivated", OWNER), (504, "cooldown", OWNER),
                (505, "session_expired", OWNER), (601, "banned", OTHER)]:
                await pool.execute(
                    "INSERT INTO tg_accounts(id, owner_id, phone, session_str, acc_status) "
                    "VALUES($1,$2,$3,$4,$5)", aid, owner, f"+7900{aid}", "sess", status)
            # Каналы владельца — для счёта.
            for cid in (701, 702):
                await pool.execute(
                    "INSERT INTO managed_channels(owner_id, acc_id, channel_id) VALUES($1,$2,$3)",
                    OWNER, 1, cid)

            app = web.Application()
            mini_app_api.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                hdr = {"X-Uid": str(OWNER)}

                # 1) Проверка: считает, не удаляет.
                r = await client.post("/api/miniapp/fleet/sweep", headers=hdr,
                                      json={"delete": False})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["bots"]["total"] == 3 and d["bots"]["dead"] == 1, d["bots"]
                assert d["bots"]["alive"] == 1 and d["bots"]["unknown"] == 1, d["bots"]
                assert d["accounts"]["dead"] == 2, d["accounts"]   # banned + session_expired
                assert d["channels"]["total"] == 2, d["channels"]
                assert d["deleted"] is False and d["deleted_total"] == 0
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", OWNER) == 3

                # 2) Preview: та же сводка, но ничего не удалено.
                r = await client.post("/api/miniapp/fleet/sweep", headers=hdr,
                                      json={"delete": True, "preview": True})
                d = await r.json()
                assert d["preview"] is True and d["deleted"] is False
                assert d["dead_total"] == 3, d   # 1 бот + 2 аккаунта (LOST)
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", OWNER) == 5

                # 3) Удаление: только мёртвое, параллельно по типам.
                r = await client.post("/api/miniapp/fleet/sweep", headers=hdr,
                                      json={"delete": True, "preview": False})
                d = await r.json()
                assert d["deleted"] is True, d
                assert d["bots"]["deleted"] == 1 and d["accounts"]["deleted"] == 2, d
                assert d["deleted_total"] == 3, d

                bots_left = {r["bot_id"] for r in await pool.fetch(
                    "SELECT bot_id FROM managed_bots WHERE added_by=$1", OWNER)}
                assert bots_left == {301, 303}, f"удалён не тот бот: {bots_left}"
                accs_left = {r["id"] for r in await pool.fetch(
                    "SELECT id FROM tg_accounts WHERE owner_id=$1", OWNER)}
                # 501 рабочий, 504 кулдаун, 505 session_expired — все восстановимы.
                assert accs_left == {501, 504, 505}, f"удалён не тот аккаунт: {accs_left}"

                # Чужое не тронуто.
                assert await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE bot_id=401") == 1
                assert await pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE id=601") == 1

                # 4) Повтор удаления идемпотентен: мёртвых больше нет.
                r = await client.post("/api/miniapp/fleet/sweep", headers=hdr,
                                      json={"delete": True, "preview": False})
                d = await r.json()
                assert d["deleted_total"] == 0, d
            finally:
                await client.close()

            await pool.execute("DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])", [OWNER, OTHER])
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])", [OWNER, OTHER])
            await pool.execute("DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])", [OWNER, OTHER])
        finally:
            await pool.close()

    asyncio.run(go())
