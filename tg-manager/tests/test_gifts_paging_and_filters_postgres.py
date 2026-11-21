"""Подарки: страницы, срез «можно передать» и срез по аккаунту.

Раньше экран получал последние 100 подарков без смещения: при большом флоте
плитка писала «340 подарков», а 101-й нельзя было ни увидеть, ни передать.
Передаваемые приходилось выискивать листанием среди непередаваемых.

Запросы новые, поэтому проверка идёт на настоящей базе.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_html, miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990801, 990802


def test_screen_has_filters_and_more():
    assert 'id="giftsFilter"' in miniapp_html() and 'id="giftsMore"' in miniapp_html()
    ui = miniapp_source()
    for fn in ("giftsSetFilter", "giftsLoadMore", "giftsFilterRender", "_giftsQuery"):
        assert f"function {fn}(" in ui, fn


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M
        from services import mini_app_stars as S

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER

        async def clean():
            await pool.execute("DELETE FROM gift_inventory WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
        try:
            await clean()
            await pool.executemany(
                "INSERT INTO platform_users(user_id) VALUES($1) ON CONFLICT DO NOTHING",
                [(OWNER,), (STRANGER,)])
            accs = []
            for who, name in ((OWNER, "Анна"), (OWNER, "Борис"), (STRANGER, "Чужой")):
                accs.append(await pool.fetchval(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, first_name)"
                    " VALUES($1, $2, 'x', $3) RETURNING id", who, f"+7999{len(accs)}", name))
            rows = [(OWNER, accs[0], f"a{i}", i % 3 == 0) for i in range(130)]
            rows += [(OWNER, accs[1], f"b{i}", True) for i in range(5)]
            rows += [(STRANGER, accs[2], "x0", True)]
            await pool.executemany(
                "INSERT INTO gift_inventory(owner_id, account_id, gift_id, gift_type, is_transferable)"
                " VALUES($1,$2,$3,'Мишка',$4)", rows)
            app = web.Application()
            S.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, accs)
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            try:
                await clean()
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_pages_reach_every_gift_and_filters_count_honestly():
    async def body(c, accs):
        seen = []
        for off in (0, 100):
            d = await (await c.get(f"/api/miniapp/gifts?offset={off}")).json()
            assert d["total"] == 135 and d["shown_total"] == 135
            seen += [g["id"] for g in d["items"]]
        assert len(seen) == 135 == len(set(seen)), "страницы теряют или повторяют подарки"

        d = await (await c.get("/api/miniapp/gifts?only=transferable")).json()
        assert d["shown_total"] == 44 + 5 and all(g["is_transferable"] for g in d["items"])

        d = await (await c.get(f"/api/miniapp/gifts?account_id={accs[1]}")).json()
        assert d["shown_total"] == 5 and {g["account_id"] for g in d["items"]} == {accs[1]}

        d = await (await c.get(f"/api/miniapp/gifts?account_id={accs[2]}")).json()
        assert d["shown_total"] == 0, "чужой аккаунт в срезе — утечка"

        by = {a["account_id"]: a for a in d["accounts"]}
        assert set(by) == {accs[0], accs[1]}
        assert by[accs[0]]["n"] == 130 and by[accs[0]]["transferable"] == 44
        assert by[accs[1]]["name"] == "Борис"
    _run(body)
