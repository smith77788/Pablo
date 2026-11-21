"""Шаблоны постов: правка, поиск и страницы; общие папки: «собрать заново»;
рассылка: «повторить с правками» и шаблон прямо из формы.

Раньше шаблон можно было только удалить и создать заново, список отдавал
последние 30 без смещения (31-й не открывался ничем), а поиска не было. У
общей папки не было ни «поделиться», ни «собрать заново»: состав
(`chat_ids`) экрану не отдавался вовсе. Рассылку нельзя было повторить с
другим текстом (только «недоставленным»), а форма рассылки не умела ни
вставить шаблон, ни сохранить текст в шаблоны.

Запросы новые, поэтому проверка идёт на настоящей базе.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990781, 990782


def test_screen_has_edit_copy_preview_search_and_folder_actions():
    ui = miniapp_source()
    for fn in ("function openTplEdit(", "function previewTpl(", "function useTplById(",
               "function shareFolder(", "function recomposeFolder("):
        assert fn in ui, fn
    assert 'id="tplSearch"' in ui
    for fn in ("function dupBcast(", "function pickTplInto(", "function saveTextAsTpl("):
        assert fn in ui, fn
    assert "onclick=\"dupBcast(" in ui and "pickTplInto('bcastText'" in ui
    assert "method:'PUT'" in ui.split("function saveTpl(", 1)[1][:3000]


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_broadcast as MB
        from services import mini_app_workspace as MW

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))
        try:
            await pool.execute("DELETE FROM asset_templates WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.execute("DELETE FROM chatlist_folders WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM broadcasts WHERE created_by=$1", OWNER)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", 990783)
            app = web.Application()
            MB.setup_routes(app, pool)
            MW.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool)
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            try:
                await pool.execute("DELETE FROM asset_templates WHERE owner_id = ANY($1::bigint[])",
                                   [OWNER, STRANGER])
                await pool.execute("DELETE FROM chatlist_folders WHERE owner_id=$1", OWNER)
                await pool.execute("DELETE FROM broadcasts WHERE created_by=$1", OWNER)
                await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", 990783)
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


async def _tpl(pool, owner, name, text):
    import json
    return await pool.fetchval(
        "INSERT INTO asset_templates(owner_id, asset_type, name, template)"
        " VALUES($1,'post',$2,$3::jsonb) RETURNING id",
        owner, name, json.dumps({"text": text, "buttons": [{"t": "x"}]}))


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_template_edit_keeps_other_fields_and_is_owner_scoped():
    async def body(c, pool):
        mine = await _tpl(pool, OWNER, "Старое", "старый текст")
        theirs = await _tpl(pool, STRANGER, "Чужое", "чужой текст")
        r = await c.put(f"/api/miniapp/template/{mine}", json={"name": "Новое", "text": "новый"})
        assert r.status == 200, await r.text()
        row = await pool.fetchrow("SELECT name, template FROM asset_templates WHERE id=$1", mine)
        import json
        tpl = json.loads(row["template"])
        assert row["name"] == "Новое" and tpl["text"] == "новый"
        assert tpl["buttons"] == [{"t": "x"}], "правка текста стёрла остальное"
        r = await c.put(f"/api/miniapp/template/{theirs}", json={"name": "Взлом", "text": "x"})
        assert r.status == 404
        assert await pool.fetchval("SELECT name FROM asset_templates WHERE id=$1", theirs) == "Чужое"
        r = await c.put(f"/api/miniapp/template/{mine}", json={"name": "", "text": "x"})
        assert r.status == 400
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_template_list_pages_and_searches():
    async def body(c, pool):
        for i in range(35):
            await _tpl(pool, OWNER, f"Шаблон {i}", "акция 100%" if i == 3 else "обычный")
        await _tpl(pool, STRANGER, "Чужая акция", "акция")
        seen = []
        for off in (0, 30):
            d = await (await c.get(f"/api/miniapp/templates?offset={off}")).json()
            assert d["total"] == 35
            seen += [t["id"] for t in d["templates"]]
        assert len(seen) == 35 == len(set(seen)), "страницы пропускают или повторяют шаблоны"
        d = await (await c.get("/api/miniapp/templates?q=акция")).json()
        assert [t["name"] for t in d["templates"]] == ["Шаблон 3"] and d["total"] == 1
        d = await (await c.get("/api/miniapp/templates?q=100%25")).json()
        assert d["total"] == 1, "процент в запросе должен искаться как символ"
        d = await (await c.get("/api/miniapp/templates?q=" + "%25")).json()
        assert d["total"] == 1
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_folder_list_returns_its_chats():
    async def body(c, pool):
        await pool.execute(
            "INSERT INTO chatlist_folders(owner_id, title, chat_ids, chat_count, status)"
            " VALUES($1,'Папка',$2,2,'ready')", OWNER, [11, 12])
        d = await (await c.get("/api/miniapp/chatlist_folders")).json()
        rows = d.get("folders") or d.get("items") or []
        assert rows and rows[0]["chat_ids"] == [11, 12]
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_broadcast_list_returns_buttons_for_repeat():
    import json

    async def body(c, pool):
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, added_by) VALUES('t',$1,'rb',$2)",
            990783, OWNER)
        await pool.execute(
            "INSERT INTO broadcasts(bot_id, message_text, status, created_by, buttons)"
            " VALUES($1,'Привет','done',$2,$3::jsonb)",
            990783, OWNER, json.dumps([{"text": "Сайт", "url": "https://a.ru"}]))
        d = await (await c.get("/api/miniapp/broadcasts")).json()
        b = d["broadcasts"][0]
        btns = json.loads(b["buttons"]) if isinstance(b["buttons"], str) else b["buttons"]
        assert btns == [{"text": "Сайт", "url": "https://a.ru"}]
    _run(body)
