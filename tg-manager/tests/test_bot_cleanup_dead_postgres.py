"""Проверка живости ботов и полное удаление: мёртвых сносим, неизвестных — нет.

До этой фичи у ботов в мини-аппе было только «убрать из работы»
(`is_active=FALSE`) — реального удаления и проверки «жив ли токен» не было
(см. tests/test_bot_power_one_honest_action.py: «удаления, которого в продукте
нет»). Здесь добавлены три ручки:

  • POST /api/miniapp/bots/check_all   — getMe по каждому токену, только чтение;
  • POST /api/miniapp/bots/bulk_delete — полное удаление выбранных (DELETE строки);
  • POST /api/miniapp/bots/cleanup_dead — проверить всех и удалить ТОЛЬКО мёртвых.

Главный инвариант, который стережёт этот тест: удаляется только то, про что
ТОЧНО известно, что токен отозван (getMe → 401 → 'dead'). Бот со статусом
'unknown' (Telegram не ответил: сеть/5xx/429) НЕ удаляется — иначе одна сетевая
ошибка снесла бы живой флот. Плюс скоуп владельца (added_by) и идемпотентность.

Живой Postgres обязателен: заглушка пула не проверяет связывание
`ANY($1::bigint[])` и не уследит за owner-скоупом DELETE — ровно та ошибка,
которая на юнит-тесте невидима. Инструкция по DSN — в других *_postgres.py.
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

    async def _fake_batch(session, tokens):
        return [_status_for(t) for t in tokens]
    monkeypatch.setattr(bot_api, "batch_check_alive", _fake_batch)

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute(
                "DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])",
                [OWNER, OTHER])
            # bot_id уникален глобально; token уникален. Четыре бота владельца:
            # один жив, два с отозванным токеном, один не ответил. Плюс чужой.
            bots = [
                (101, "t_alive_101", OWNER),
                (102, "t_dead_102", OWNER),
                (103, "t_unknown_103", OWNER),
                (104, "t_dead_104", OWNER),
                (201, "t_alive_201", OTHER),
            ]
            for bid, tok, owner in bots:
                await pool.execute(
                    "INSERT INTO managed_bots(bot_id, token, username, is_active, added_by) "
                    "VALUES($1,$2,$3,TRUE,$4)", bid, tok, f"bot{bid}", owner)

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
                assert d["alive"] == 1 and d["dead"] == 2 and d["unknown"] == 1, d
                by_id = {b["bot_id"]: b["status"] for b in d["bots"]}
                assert by_id[102] == "dead" and by_id[103] == "unknown", by_id
                # Ничего не удалено проверкой.
                n = await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", OWNER)
                assert n == 4, "check_all не должен удалять"

                # 2) cleanup_dead — удаляет ТОЛЬКО мёртвых (102, 104); 101 (жив)
                #    и 103 (не ответил) остаются; чужой 201 не тронут.
                r = await client.post("/api/miniapp/bots/cleanup_dead", headers=hdr, json={})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["checked"] == 4 and d["deleted"] == 2 and d["dead"] == 2, d
                assert d["unknown"] == 1 and d["alive"] == 1, d
                assert sorted(d["deleted_ids"]) == [102, 104], d
                left = {r["bot_id"] for r in await pool.fetch(
                    "SELECT bot_id FROM managed_bots WHERE added_by=$1", OWNER)}
                assert left == {101, 103}, f"удалены не те боты: {left}"
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_bots WHERE bot_id=201") == 1, \
                    "чужой бот не должен быть затронут"

                # 3) bulk_delete — скоуп владельца: 101 свой (удалится), 201 чужой
                #    (не удалится). requested=2, deleted=1.
                r = await client.post("/api/miniapp/bots/bulk_delete", headers=hdr,
                                      json={"bot_ids": [101, 201]})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["requested"] == 2 and d["deleted"] == 1, d
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_bots WHERE bot_id=101") == 0
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_bots WHERE bot_id=201") == 1, \
                    "bulk_delete вышел за owner-скоуп"

                # 4) Идемпотентность: повтор по уже удалённому = 0, не ошибка.
                r = await client.post("/api/miniapp/bots/bulk_delete", headers=hdr,
                                      json={"bot_ids": [101]})
                assert r.status == 200
                assert (await r.json())["deleted"] == 0

                # 5) Пустой выбор — явная 400, а не молчаливое удаление всего.
                r = await client.post("/api/miniapp/bots/bulk_delete", headers=hdr,
                                      json={"bot_ids": []})
                assert r.status == 400
            finally:
                await client.close()

            await pool.execute(
                "DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])",
                [OWNER, OTHER])
        finally:
            await pool.close()

    asyncio.run(go())
