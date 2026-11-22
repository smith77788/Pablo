"""Сетка ботов: «🔁 Повторить цепочку» с экрана задачи.

Одинаковую цепочку собирали заново: инициатор, каждый шаг маршрута по одному
и текст. Теперь экран задачи отдаёт маршрут и текст, и повтор — одна кнопка.
Маршрут отдаётся, только если ВСЕ его боты — боты владельца: повтор по
наполовину чужому маршруту сервер всё равно отклонит, и кнопка обещала бы
невозможное. route/payload — jsonb, asyncpg отдаёт их строкой, поэтому
проверка на живой базе.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990931, 990932
B1, B2, B3, ALIEN = 9909310001, 9909310002, 9909310003, 9909320001
JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "bot_mesh.js").read_text(encoding="utf-8")


def test_screen_offers_repeat_only_when_route_is_known():
    assert "meshCanRepeat(d)?" in JS and 'onclick="meshRepeatTask()"' in JS
    body = re.search(r"\nasync function meshRepeatTask\([\s\S]*?\n}\n", JS).group(0)
    assert "'/api/miniapp/mesh/task'" in body and "d.route_ids.map" in body
    assert "askConfirm(" in body


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_detail_returns_own_route_and_text_but_not_a_half_foreign_route():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_parser as P

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        # setup_routes берёт _get_uid при регистрации — подменяем ОДИН раз
        # функцией, которая читает текущего пользователя.
        who = [OWNER]
        M._get_uid = lambda r: who[0]
        bots = [B1, B2, B3, ALIEN]
        try:
            await pool.execute("DELETE FROM bot_mesh_tasks WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
            await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", bots)
            for bid, owner in ((B1, OWNER), (B2, OWNER), (B3, OWNER), (ALIEN, STRANGER)):
                await pool.execute(
                    "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
                    "VALUES($1,$2,$3,TRUE,$4)", f"1:mesh-repeat-{bid}", bid, f"b{bid}", owner)
            ins = ("INSERT INTO bot_mesh_tasks(task_id, owner_id, origin_bot, route, payload, deadline_at) "
                   "VALUES($1,$2,$3,$4::jsonb,$5::jsonb,$6)")
            dl = datetime.now(UTC) + timedelta(hours=1)
            await pool.execute(ins, "rep-own", OWNER, B1,
                               json.dumps([{"bot": B2, "capability": None}, {"bot": B3, "capability": None}]),
                               json.dumps({"text": "Собери отчёт"}), dl)
            await pool.execute(ins, "rep-half", OWNER, B1,
                               json.dumps([{"bot": B2, "capability": None}, {"bot": ALIEN, "capability": None}]),
                               "{}", dl)
            app = web.Application()
            P.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.get("/api/miniapp/mesh/task/rep-own")
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["route_ids"] == [B2, B3]
                assert d["text"] == "Собери отчёт"
                assert d["origin_id"] == B1

                r = await client.get("/api/miniapp/mesh/task/rep-half")
                d = await r.json()
                assert d["route_ids"] == [], "наполовину чужой маршрут обещал бы повтор"
                assert d["text"] == ""

                # Чужая задача — не найдена.
                who[0] = STRANGER
                r = await client.get("/api/miniapp/mesh/task/rep-own")
                assert r.status == 404
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await pool.execute("DELETE FROM bot_mesh_tasks WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
            await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", bots)
            await pool.close()

    asyncio.run(go())
