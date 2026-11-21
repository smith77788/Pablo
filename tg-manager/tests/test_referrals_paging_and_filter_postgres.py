"""Рефералы: страницы и срез по состоянию.

Экран писал «Рефералов: 57», а список отдавал последние 20 без смещения —
21-го приглашённого нельзя было увидеть. Теперь страницы с честным итогом
среза и чипы «Оплатили / Активны / Без активности», которые считает сервер.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_html, miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990811
BASE = 990900


def test_screen_has_chips_and_more():
    html = miniapp_html()
    assert 'id="referralChips"' in html and 'id="referralMore"' in html
    ui = miniapp_source()
    for fn in ("refRenderList", "refFilter", "refLoadMore", "_refQuery"):
        assert f"function {fn}(" in ui, fn


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        ids = [BASE + i for i in range(30)]

        async def clean():
            await pool.execute("DELETE FROM platform_referrals WHERE referrer_id=$1", OWNER)
            await pool.execute("DELETE FROM platform_users WHERE user_id = ANY($1::bigint[])",
                               [OWNER, *ids])
        try:
            await clean()
            await pool.executemany(
                "INSERT INTO platform_users(user_id, username) VALUES($1,$2)",
                [(OWNER, "owner"), *[(i, f"u{i}") for i in ids]])
            # 30 рефералов: 5 оплатили, 10 активны, 15 без активности.
            await pool.executemany(
                "INSERT INTO platform_referrals(id, referrer_id, referred_id, created_at,"
                " activated_at, paid_at) VALUES($1,$2,$3, now() - $4 * interval '1 minute',"
                " CASE WHEN $5 THEN now() END, CASE WHEN $6 THEN now() END)",
                [(BASE + k, OWNER, i, k, k < 15, k < 5) for k, i in enumerate(ids)])
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client)
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
def test_pages_reach_everyone_and_status_filter_counts():
    async def body(c):
        seen = []
        for off in (0, 20):
            d = await (await c.get(f"/api/miniapp/referral/detail?offset={off}")).json()
            assert d["referral_count"] == 30 and d["shown_total"] == 30
            seen += [r["id"] for r in d["referrals"]]
        assert len(seen) == 30 == len(set(seen)), "страницы теряют или повторяют рефералов"
        for st, n in (("paid", 5), ("active", 10), ("pending", 15)):
            d = await (await c.get(f"/api/miniapp/referral/detail?status={st}&limit=100")).json()
            assert d["shown_total"] == n == len(d["referrals"]), st
            assert {r["status"] for r in d["referrals"]} == {st}
    _run(body)
