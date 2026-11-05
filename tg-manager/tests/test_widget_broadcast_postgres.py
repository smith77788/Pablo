"""Рассылка HTML-виджета подписчикам бота — на живой базе.

Виджет рассылается ОБЫЧНОЙ рассылкой бота (журнал доставок, стоп, докатка
после рестарта), только каждому уходит sendSharedWidget: у всех получателей
один общий виджет и один счёт голосов. Признак виджета живёт в строке
рассылки (broadcasts.widget_id), и опасность здесь одна: потерять его. Тогда
рассылка уйдёт аудитории подписью «🧩 Виджет: …» обычным текстом. Поэтому:

* рассылка со строкой-виджетом шлёт sendSharedWidget и ни одного sendMessage,
  пропуская уже доставленных;
* обычная рассылка по-прежнему шлёт текст (контроль);
* повтор недоставленным остаётся виджетом;
* ручка экрана пишет признак тем же INSERT, что и строку рассылки, и ставит
  операцию run_broadcast.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991861
BOT = 7718610001
TOKEN = "7718610001:AAHwidget-fixture_0123456789abcdefgh"
SUBS = (61001, 61002, 61003)

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4,
                                      server_settings={"lock_timeout": "3000"})
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if os.path.exists(path):
                try:
                    await p.execute(open(path, encoding="utf-8").read())
                except Exception:
                    pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(p.close())


@pytest.fixture(autouse=True)
def audience(pool):
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(pool.execute("INSERT INTO managed_bots(token, bot_id, username, added_by) "
                      "VALUES($1,$2,'widget_fixture',$3)", TOKEN, BOT, OWNER))
    _run(pool.executemany(
        "INSERT INTO bot_users(bot_id, user_id, language_code, is_active, is_blocked, suspect) "
        "VALUES($1,$2,'ru',TRUE,FALSE,FALSE)", [(BOT, u) for u in SUBS]))
    yield
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))


class _Session:
    async def close(self):
        return None


def _fake_telegram(monkeypatch):
    from services import bot_api
    calls = []

    async def _call(session, token, method, *, api_base=None, **params):
        calls.append((method, params))
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT, "username": "widget_fixture"}}
        return {"ok": True, "result": {"message_id": len(calls), "chat": {"id": params.get("chat_id")}}}

    monkeypatch.setattr(bot_api, "_call", _call)

    async def _free(*a, **k):
        return False

    monkeypatch.setattr("services.brand_injection.is_free_tier", _free)
    return calls


def _bcast(pool, widget_id=None) -> int:
    return _run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, created_by, widget_id) "
        "VALUES($1,$2,3,$3,$4) RETURNING id", BOT, "🧩 Виджет: Опрос", OWNER, widget_id))


def test_a_widget_broadcast_sends_the_shared_widget_not_text(pool, monkeypatch):
    from services import broadcaster

    calls = _fake_telegram(monkeypatch)
    bc = _bcast(pool, "w-shared-1")
    _run(pool.execute("INSERT INTO broadcast_delivery_log(broadcast_id, user_id) VALUES($1,$2)",
                      bc, SUBS[0]))
    _run(broadcaster.run(pool, _Session(), bc, TOKEN, BOT, "🧩 Виджет: Опрос"))
    sends = [(m, p) for m, p in calls if m.startswith("send")]
    assert [m for m, _ in sends] == ["sendSharedWidget", "sendSharedWidget"], sends
    assert sorted(p["chat_id"] for _, p in sends) == list(SUBS[1:]), "уже доставленному — второй раз"
    assert all(p["widget_id"] == "w-shared-1" for _, p in sends)
    delivered = _run(pool.fetchval(
        "SELECT COUNT(*) FROM broadcast_delivery_log WHERE broadcast_id=$1", bc))
    assert delivered == 3


def test_a_text_broadcast_still_sends_text(pool, monkeypatch):
    from services import broadcaster

    calls = _fake_telegram(monkeypatch)
    bc = _bcast(pool, None)
    _run(broadcaster.run(pool, _Session(), bc, TOKEN, BOT, "Новость дня"))
    assert {m for m, _ in calls if m.startswith("send")} == {"sendMessage"}


def test_resend_to_the_undelivered_stays_a_widget(pool, monkeypatch):
    from services import broadcaster, operation_bus

    submitted = []

    async def _submit(pool_, owner, op_type, params, **kw):
        submitted.append((op_type, params))
        return 1

    monkeypatch.setattr(operation_bus, "submit", _submit)
    bc = _bcast(pool, "w-shared-2")
    _run(pool.execute("INSERT INTO broadcast_delivery_log(broadcast_id, user_id) VALUES($1,$2)",
                      bc, SUBS[0]))
    res = _run(broadcaster.resend_undelivered(pool, OWNER, bc))
    assert res["ok"], res
    wid = _run(pool.fetchval("SELECT widget_id FROM broadcasts WHERE id=$1", res["broadcast_id"]))
    assert wid == "w-shared-2", "повтор ушёл бы недоставленным текстом"
    assert submitted and sorted(submitted[0][1]["user_ids"]) == list(SUBS[1:])


def test_the_screen_marks_the_broadcast_as_a_widget_in_one_insert(pool, monkeypatch):
    async def go():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M
        from services import mini_app_widgets as W
        from services import operation_bus

        submitted = []

        async def _submit(pool_, owner, op_type, params, **kw):
            row = await pool_.fetchrow("SELECT status, widget_id FROM broadcasts WHERE id=$1",
                                       params["broadcast_id"])
            submitted.append((op_type, params, dict(row)))
            return 77

        monkeypatch.setattr(operation_bus, "submit", _submit)
        monkeypatch.setattr(M, "_get_uid", lambda r: OWNER)
        wrow = await pool.fetchval(
            "INSERT INTO bot_widgets(owner_id, bot_id, chat_id, message_id, widget_id, template, title,"
            " public_data, allow_queries) VALUES($1,$2,$1,5,'w-shared-3','poll','Куда летом?','{}',TRUE)"
            " RETURNING id", OWNER, BOT)
        app = web.Application()
        W.setup_routes(app, pool)
        async with TestClient(TestServer(app)) as client:
            r = await client.post(f"/api/miniapp/widgets/{wrow}/broadcast", json={"segment": "all"})
            body = await r.json()
            assert r.status == 200, body
            assert body["total"] == 3
            op_type, params, row = submitted[0]
            assert op_type == "run_broadcast" and params["widget_id"] == "w-shared-3"
            assert row == {"status": "pending", "widget_id": "w-shared-3"}
            r = await client.post(f"/api/miniapp/widgets/{wrow}/broadcast", json={"segment": "nope"})
            assert r.status == 400

    _run(go())
