"""Массовое «убрать канал из управления»: несколько разом, строго по владельцу.

До этого у каналов было только одиночное «убрать» (channel_remove) —
убрать несколько выбранных разом было нельзя. Добавлен
POST /api/miniapp/channels/bulk_delete: DELETE строк managed_channels по
owner-скоупу, идемпотентно.

Инвариант, который стережёт тест: удаление идёт ТОЛЬКО по строкам владельца,
даже когда channel_id совпадает у двух владельцев (managed_channels UNIQUE
по (owner_id, channel_id), не по одному channel_id). Живой Postgres: заглушка
пула не проверит ни связывание ANY($1::bigint[]), ни owner-скоуп.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 7770003
OTHER = 8880004


async def _load_schema(conn) -> None:
    for f in ["schema.sql"] + sorted(
        glob.glob("schema_v*.sql"),
        key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)),
    ):
        try:
            await conn.execute(open(f, encoding="utf-8").read())
        except Exception:
            pass


def test_bulk_delete_is_owner_scoped_and_idempotent(monkeypatch):
    import asyncio

    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api
    from services import mini_app_channels

    monkeypatch.setattr(mini_app_api, "_get_uid",
                        lambda request: int(request.headers["X-Uid"])
                        if request.headers.get("X-Uid") else None)
    from database import db as _db

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(_db, "record_manual_action", _noop)

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute(
                "DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])",
                [OWNER, OTHER])
            # channel_id 555 нарочно у ОБОИХ владельцев: owner-скоуп обязан
            # удалить только строку OWNER, чужую 555 не тронуть.
            chans = [
                (OWNER, 1, 555, "A"),
                (OWNER, 1, 556, "B"),
                (OWNER, 1, 557, "C"),
                (OTHER, 2, 555, "foreign"),
            ]
            for owner, acc, cid, title in chans:
                await pool.execute(
                    "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title) "
                    "VALUES($1,$2,$3,$4)", owner, acc, cid, title)

            app = web.Application()
            mini_app_channels.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                hdr = {"X-Uid": str(OWNER)}

                # Удаляем 555 и 556 владельца + чужой-по-id 555 в наборе.
                r = await client.post("/api/miniapp/channels/bulk_delete", headers=hdr,
                                      json={"channel_ids": [555, 556]})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["requested"] == 2 and d["deleted"] == 2, d

                own = {r["channel_id"] for r in await pool.fetch(
                    "SELECT channel_id FROM managed_channels WHERE owner_id=$1", OWNER)}
                assert own == {557}, f"у владельца остаться должен только 557: {own}"
                # Чужая 555 на месте — owner-скоуп соблюдён.
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND channel_id=555",
                    OTHER) == 1, "удаление вышло за owner-скоуп"

                # Идемпотентность: повтор по уже убранным = 0.
                r = await client.post("/api/miniapp/channels/bulk_delete", headers=hdr,
                                      json={"channel_ids": [555, 556]})
                assert (await r.json())["deleted"] == 0

                # Пустой список — явная 400, не молчаливое удаление всего.
                r = await client.post("/api/miniapp/channels/bulk_delete", headers=hdr,
                                      json={"channel_ids": []})
                assert r.status == 400
            finally:
                await client.close()

            await pool.execute(
                "DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])",
                [OWNER, OTHER])
        finally:
            await pool.close()

    asyncio.run(go())
