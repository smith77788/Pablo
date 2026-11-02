"""Сделку из бота можно создать и провести по всем стадиям — на настоящей базе.

Проверка crm_deals_stage_check ставится ровно так, как её ставит
services/mini_app_api.py, и бот проходит каждую свою стадию через
db.create_crm_deal / db.move_crm_deal_stage. До исправления первая же вставка
со stage='new' падала на этой проверке.

Нужен живой Postgres (INFRAGRAM_TEST_DSN), иначе тест пропускается.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993811


def test_bot_deal_walks_every_stage():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    from bot.handlers import crm
    from database import db

    async def go():
        try:
            conn = await asyncpg.connect(DSN)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await conn.execute("DELETE FROM crm_deals WHERE owner_id=$1", OWNER)
            await conn.execute("ALTER TABLE crm_deals DROP CONSTRAINT IF EXISTS crm_deals_stage_check")
            await conn.execute(
                "ALTER TABLE crm_deals ADD CONSTRAINT crm_deals_stage_check "
                "CHECK (stage IN ('lead','contact','proposal','negotiation','won','lost'))")
            deal = await db.create_crm_deal(conn, OWNER, "Тест", contact="@x", stage="new")
            row = await db.get_crm_deal(conn, deal, OWNER)
            assert row["stage"] == "lead"
            for st in crm._PIPELINE_STAGES:
                await db.move_crm_deal_stage(conn, deal, OWNER, st)
                assert (await db.get_crm_deal(conn, deal, OWNER))["stage"] == st
            stats = await db.get_crm_dashboard_stats(conn, OWNER)
            assert set(crm._PIPELINE_STAGES) <= set(stats)
            assert stats[crm._PIPELINE_STAGES[-1]]["count"] == 1
        finally:
            await conn.execute("DELETE FROM crm_deals WHERE owner_id=$1", OWNER)
            await conn.close()

    asyncio.new_event_loop().run_until_complete(go())
