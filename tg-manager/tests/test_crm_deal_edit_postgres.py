"""Доска сделок: правка сделки без пересоздания.

Опечатка в сумме или названии простой сделки лечилась только удалением и
созданием заново — сделка теряла дату и место в истории. Теперь есть правка;
запрос новый, поэтому проверка на живой базе: владелец, проверки, валюта и
то, что обрезанные доской контакт и заметки не стираются, если их не правили.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990861, 990862
LONG_NOTES = "з" * 500


def test_board_card_offers_edit_and_form_knows_edit_mode():
    ui = miniapp_source()
    assert "dkEditDeal(' + id + ')" in ui
    assert "openDealModal(f.deal)" in ui
    assert "'/api/miniapp/crm/deal/' + editId, {method:'PUT'" in ui
    # Нетронутые обрезанные поля не отправляются.
    assert "if (body.notes === (orig.notes || '')) delete body.notes;" in ui


def test_board_marks_cut_deals():
    from services.mini_app_deals_board import deal_payload
    row = {"id": 1, "stage": "lead", "title": "x", "contact": "", "value": 0,
           "currency": "RUB", "notes": LONG_NOTES, "updated_at": None}
    assert deal_payload(row, "RUB")["cut"] is True
    assert deal_payload(dict(row, notes="коротко"), "RUB")["cut"] is False


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_edit_is_owner_scoped_validated_and_keeps_untouched_fields():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_broadcast as B

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        try:
            await pool.execute("DELETE FROM crm_deals WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            ins = ("INSERT INTO crm_deals(owner_id, title, contact, stage, value, notes, currency) "
                   "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id")
            mine = await pool.fetchval(ins, OWNER, "Подпска", "@ivan", "proposal", 100, LONG_NOTES, "RUB")
            alien = await pool.fetchval(ins, STRANGER, "Чужая", None, "lead", 5, None, "RUB")
            app = web.Application()
            B.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                # Заметки не присланы — остаются целыми; стадия не меняется.
                r = await client.put(f"/api/miniapp/crm/deal/{mine}",
                                     json={"title": " Подписка ", "contact": "@petr", "value": 150,
                                           "currency": "usd", "stage": "won"})
                assert r.status == 200, await r.text()
                row = await pool.fetchrow(
                    "SELECT title, contact, value, notes, currency, stage FROM crm_deals WHERE id=$1", mine)
                assert row["title"] == "Подписка" and row["contact"] == "@petr"
                assert float(row["value"]) == 150 and row["currency"] == "USD"
                assert row["notes"] == LONG_NOTES and row["stage"] == "proposal"
                # Присланные пустые заметки стираются — это правка владельца.
                r = await client.put(f"/api/miniapp/crm/deal/{mine}",
                                     json={"title": "Подписка", "value": 150, "notes": ""})
                assert r.status == 200
                assert await pool.fetchval("SELECT notes FROM crm_deals WHERE id=$1", mine) is None
                # Чужая сделка — 404 и без изменений.
                r = await client.put(f"/api/miniapp/crm/deal/{alien}", json={"title": "Моя"})
                assert r.status == 404
                assert await pool.fetchval("SELECT title FROM crm_deals WHERE id=$1", alien) == "Чужая"
                for bad in ({"title": "  "}, {"title": "x", "value": -1}, {"title": "x", "currency": "ZZZ"}):
                    r = await client.put(f"/api/miniapp/crm/deal/{mine}", json=bad)
                    assert r.status == 400, bad
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await pool.execute("DELETE FROM crm_deals WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
