"""Радар ниши: «Не наше» обратимо, каталог листается дальше первых 60.

«Не наше» было необратимым, а каталог показывал первые 60 ресурсов и писал,
что остальные есть, — но посмотреть их было нельзя. Возврат — новый запрос,
поэтому проверка на живой базе: только своё, только ручной пропуск, и статус
возвращается тот, что был бы без пропуска.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC
from pathlib import Path

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990891, 990892
JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "niche.js").read_text(encoding="utf-8")


def test_catalog_pages_and_offers_undo():
    assert "onclick=\"nrMoreAssets(this)\"" in JS
    assert "&offset=${Number(NR.assetOffset) || 0}" in JS
    assert "onclick=\"nrUnskip(${Number(" in JS


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_unskip_restores_only_manual_skips_of_the_owner():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_niche as N

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        owners = [OWNER, STRANGER]
        try:
            await pool.execute("DELETE FROM niche_assets WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.execute("DELETE FROM niche_campaigns WHERE owner_id = ANY($1::bigint[])", owners)
            cid = await pool.fetchval(
                "INSERT INTO niche_campaigns(owner_id, niche) VALUES($1, 'кофе') RETURNING id", OWNER)
            ins = ("INSERT INTO niche_assets(owner_id, campaign_id, tg_id, username, status, skip_reason, joined_at) "
                   "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id")
            from datetime import datetime
            now = datetime.now(UTC)
            fresh = await pool.fetchval(ins, OWNER, cid, 1, "a", "found", "", None)
            was_joined = await pool.fetchval(ins, OWNER, cid, 2, "b", "joined", "", now)
            by_rule = await pool.fetchval(ins, OWNER, cid, 3, "c", "skipped", "мало участников", None)
            alien = await pool.fetchval(ins, STRANGER, None, 4, "d", "skipped", "убрано вручную", None)
            for i in range(5, 70):
                await pool.execute(ins, OWNER, cid, i, "x" + str(i), "found", "", None)
            app = web.Application()
            N.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                st = "SELECT status, skip_reason FROM niche_assets WHERE id=$1"
                # «Не наше» и возврат: без вступления — «Найден», со вступлением — «Вступили».
                for aid, back_to in ((fresh, "found"), (was_joined, "joined")):
                    r = await client.post(f"/api/miniapp/niche/asset/{aid}/skip", json={})
                    assert r.status == 200
                    assert (await pool.fetchrow(st, aid))["status"] == "skipped"
                    r = await client.post(f"/api/miniapp/niche/asset/{aid}/unskip", json={})
                    assert r.status == 200, await r.text()
                    assert (await r.json())["status"] == back_to
                    row = await pool.fetchrow(st, aid)
                    assert (row["status"], row["skip_reason"]) == (back_to, "")
                # Пропуск по правилам радара и чужой ресурс — не возвращаются.
                for aid in (by_rule, alien):
                    r = await client.post(f"/api/miniapp/niche/asset/{aid}/unskip", json={})
                    assert r.status == 404
                    assert (await pool.fetchrow(st, aid))["status"] == "skipped"
                # Каталог листается: вторая страница продолжает первую без повторов.
                base = f"/api/miniapp/niche/campaign/{cid}/assets?limit=60&status="
                p1 = await (await client.get(base + "&offset=0")).json()
                p2 = await (await client.get(base + "&offset=60")).json()
                assert p1["has_more"] is True and p2["has_more"] is False
                ids = [a["id"] for a in p1["items"]] + [a["id"] for a in p2["items"]]
                assert len(ids) == len(set(ids)) == p1["total"] == 68
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await pool.execute("DELETE FROM niche_assets WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.execute("DELETE FROM niche_campaigns WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
