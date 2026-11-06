"""HTML-виджеты на настоящей базе: голоса, чужие боты, правка, ответы.

* голос переключается: второе касание того же варианта снимает голос, другой
  вариант переносит его; номер правки растёт на каждом голосе;
* «кто идёт» отдаёт имена идущих;
* касание из пересланной копии (по widget_id) попадает в тот же счёт;
* боту отвечают ВСЕГДА — и когда правка у Telegram не прошла, и когда виджет
  неизвестен (иначе виджет 15 секунд ждёт и пишет «Бот не ответил»);
* отправить от чужого или выключенного бота нельзя, чужой виджет — 404;
* правка отправленного опроса меняет текст и сохраняет голоса.

Запросы новые (FOR UPDATE, ON CONFLICT, агрегаты) — поэтому на живом Postgres.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER, STRANGER = 990401, 990402
BOT, OFF_BOT, ALIEN_BOT = 7001, 7002, 7003
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _db(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(DSN)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


class _Telegram:
    """Подменный Telegram: записывает вызовы, отвечает как настоящий."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.next_id = 100

    async def __call__(self, session, token, method, *, api_base=None, **params):
        self.calls.append((method, {k: v for k, v in params.items() if v is not None}))
        if method in self.fail:
            return {"ok": False, "error_code": 400, "description": "Bad Request: WIDGET_ID_INVALID"}
        if method in ("sendWidget", "sendSharedWidget"):
            self.next_id += 1
            chat = params["chat_id"]
            return {"ok": True, "result": {"message_id": self.next_id,
                                           "chat": {"id": chat if isinstance(chat, int) else -1009},
                                           "widget": {"id": f"w{self.next_id}"}}}
        return {"ok": True, "result": True}


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import bot_widgets as bw
        from services import mini_app_api as M
        from services import mini_app_widgets as W

        name = "wg_" + uuid.uuid4().hex[:10]
        root = await asyncpg.connect(DSN)
        await root.execute(f'CREATE DATABASE "{name}"')
        pool = None
        tg = _Telegram()
        orig = (M._get_uid, bw.bot_api._call)
        uid_box = [OWNER]
        M._get_uid = lambda r: uid_box[0]
        bw.bot_api._call = tg
        try:
            pool = await asyncpg.create_pool(_db(name), min_size=1, max_size=4)
            await pool.execute(
                "CREATE TABLE managed_bots(bot_id bigint PRIMARY KEY, token text, username text,"
                " added_by bigint, is_active boolean DEFAULT true)")
            await pool.execute((ROOT / "schema_v301_bot_widgets.sql").read_text(encoding="utf-8"))
            await pool.executemany(
                "INSERT INTO managed_bots VALUES($1,$2,$3,$4,$5)",
                [(BOT, "1:a", "shop_bot", OWNER, True), (OFF_BOT, "2:b", "off_bot", OWNER, False),
                 (ALIEN_BOT, "3:c", "alien_bot", STRANGER, True)])
            app = web.Application()
            W.setup_routes(app, pool)
            async with TestClient(TestServer(app)) as client:
                return await body(pool, client, tg, uid_box)
        finally:
            M._get_uid, bw.bot_api._call = orig
            if pool is not None:
                await pool.close()
            await root.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            await root.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


async def _send(client, **body):
    r = await client.post("/api/miniapp/widgets/send", json=body)
    return r.status, await r.json()


def _q(qid, data, user, *, widget_id=None, chat=None, message_id=None):
    q = {"id": qid, "data": data, "from": user}
    if widget_id:
        q["widget_id"] = widget_id
    if chat is not None:
        q["chat"] = {"id": chat}
        q["message_id"] = message_id
    return q


def test_votes_toggle_count_and_always_get_an_answer():
    async def body(pool, client, tg, uid_box):
        from services import bot_widget_runtime as rt

        st, d = await _send(client, bot_id=BOT, chat="me", template="poll",
                            values={"q": "Куда летом?", "o": ["Море", "Горы", "Дача"]})
        assert st == 200 and d["ok"], d
        sent = [c for c in tg.calls if c[0] == "sendWidget"][-1][1]
        assert sent["allow_queries"] is True and sent["is_shared"] is True
        row = await pool.fetchrow("SELECT * FROM bot_widgets WHERE id=$1", d["id"])
        assert row["chat_id"] == OWNER and row["widget_id"]
        wid = row["widget_id"]
        anna = {"id": 11, "first_name": "Анна"}
        boris = {"id": 12, "first_name": "Борис"}

        a = await rt.on_query(pool, None, "1:a", BOT, _q("q1", "vote:1", anna, widget_id=wid))
        assert a == {"my": 1, "v": [0, 1, 0], "r": 1}
        a = await rt.on_query(pool, None, "1:a", BOT, _q("q2", "vote:1", boris, widget_id=wid))
        assert a["v"] == [0, 2, 0] and a["r"] == 2
        # Анна передумала — голос переезжает, а не добавляется.
        a = await rt.on_query(pool, None, "1:a", BOT, _q("q3", "vote:0", anna, widget_id=wid))
        assert a == {"my": 0, "v": [1, 1, 0], "r": 3}
        # Повторное касание того же варианта снимает голос.
        a = await rt.on_query(pool, None, "1:a", BOT, _q("q4", "vote:0", anna, widget_id=wid))
        assert a == {"my": -1, "v": [0, 1, 0], "r": 4}
        # Правка доходит до всех копий, ответ уходит на каждый запрос.
        shared = [c for c in tg.calls if c[0] == "editSharedWidgetData"]
        assert len(shared) == 4 and json.loads(shared[-1][1]["public_data"])["v"] == [0, 1, 0]
        answers = [c[1] for c in tg.calls if c[0] == "answerWidgetQuery"]
        assert [x["widget_query_id"] for x in answers] == ["q1", "q2", "q3", "q4"]
        stored = json.loads(await pool.fetchval("SELECT public_data FROM bot_widgets WHERE id=$1", d["id"]))
        assert stored["v"] == [0, 1, 0] and stored["r"] == 4 and stored["q"] == "Куда летом?"

        # Telegram правку не принял — голос всё равно принят и ответ ушёл.
        tg.fail.add("editSharedWidgetData")
        a = await rt.on_query(pool, None, "1:a", BOT, _q("q5", "vote:2", anna, widget_id=wid))
        assert a["my"] == 2
        tg.fail.clear()
        # Неизвестный виджет, мусорное касание, вариант за пределами — ответ с причиной.
        for q in (_q("q6", "vote:0", anna, widget_id="nope"),
                  _q("q7", "drop table", anna, widget_id=wid),
                  _q("q8", "vote:7", anna, widget_id=wid)):
            a = await rt.on_query(pool, None, "1:a", BOT, q)
            assert "err" in a
        answered = {c[1]["widget_query_id"] for c in tg.calls if c[0] == "answerWidgetQuery"}
        assert {"q5", "q6", "q7", "q8"} <= answered
        # Чужой бот не может тронуть голоса этого виджета.
        a = await rt.on_query(pool, None, "3:c", ALIEN_BOT, _q("q9", "vote:0", boris, widget_id=wid))
        assert "err" in a

        # Список владельца видит виджет и число голосов.
        r = await client.get("/api/miniapp/widgets?limit=10&offset=0")
        lst = await r.json()
        assert lst["total"] == 1 and lst["items"][0]["votes"] == 2

        # Правка текста сохраняет голоса; варианты поменять нельзя.
        r = await client.post(f"/api/miniapp/widgets/{d['id']}/edit",
                              json={"values": {"q": "Куда поедем?", "o": ["Море", "Горы", "Дача"]}})
        assert r.status == 200, await r.text()
        stored = json.loads(await pool.fetchval("SELECT public_data FROM bot_widgets WHERE id=$1", d["id"]))
        assert stored["q"] == "Куда поедем?" and stored["v"] == [0, 1, 1]
        r = await client.post(f"/api/miniapp/widgets/{d['id']}/edit",
                              json={"values": {"q": "?", "o": ["Горы", "Море", "Дача"]}})
        assert r.status == 400 and "нельзя" in (await r.json())["error"]
    _run(body)


def test_rsvp_answers_carry_the_names_of_those_going():
    async def body(pool, client, tg, uid_box):
        from services import bot_widget_runtime as rt

        st, d = await _send(client, bot_id=BOT, chat="@party_group", template="rsvp",
                            values={"t": "Шашлыки", "w": "суббота"})
        assert st == 200, d
        row = await pool.fetchrow("SELECT * FROM bot_widgets WHERE id=$1", d["id"])
        for i, name in enumerate(("Анна", "Борис", "Вера")):
            a = await rt.on_query(pool, None, "1:a", BOT,
                                  _q(f"r{i}", "vote:0", {"id": 20 + i, "first_name": name},
                                     chat=row["chat_id"], message_id=row["message_id"]))
        assert a["v"] == [3, 0, 0] and a["n"] == ["Анна", "Борис", "Вера"]
    _run(body)


def test_strangers_and_switched_off_bots_are_refused():
    async def body(pool, client, tg, uid_box):
        for bot in (ALIEN_BOT, OFF_BOT, "abc"):
            st, d = await _send(client, bot_id=bot, chat="me", template="card",
                                values={"t": "Акция"})
            assert st == 404, (bot, d)
        st, d = await _send(client, bot_id=BOT, chat="мой чат", template="card",
                            values={"t": "Акция"})
        assert st == 400 and "Куда отправить" in d["error"]
        st, d = await _send(client, bot_id=BOT, chat="me", template="card", values={"t": ""})
        assert st == 400 and "Заголовок" in d["error"]
        assert not [c for c in tg.calls if c[0] == "sendWidget"], "отказ — до Telegram"

        st, d = await _send(client, bot_id=BOT, chat="me", template="card", values={"t": "Акция"})
        assert st == 200
        uid_box[0] = STRANGER
        for path in (f"/api/miniapp/widgets/{d['id']}/edit", f"/api/miniapp/widgets/{d['id']}/delete"):
            r = await client.post(path, json={"values": {"t": "Взлом"}})
            assert r.status == 404, path
        r = await client.get("/api/miniapp/widgets")
        assert (await r.json())["total"] == 0
        uid_box[0] = OWNER
        r = await client.post(f"/api/miniapp/widgets/{d['id']}/delete", json={})
        assert r.status == 200
        assert [c for c in tg.calls if c[0] == "deleteMessage"]
        assert await pool.fetchval("SELECT COUNT(*) FROM bot_widgets") == 0
    _run(body)
