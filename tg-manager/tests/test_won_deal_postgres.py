"""Выигранная сделка на живом Postgres: купивший уходит из «кого дожимать».

Чистые функции и обе двери проверены в test_won_deal_reaches_the_layer. Здесь —
реальный путь целиком: владелец ставит контакту стадию «Выиграно» на экране
контакта, и список «кого дожимать первыми» в сводке слоя его больше не
показывает. Заглушка пула этого не покажет: `contact_crm.contact_id` — uuid, а
`virtual_states.entity_id` — текст, и связывание типов видно только на живой базе.

Без INFRAGRAM_TEST_DSN — скип. Инструкция по стенду — в
tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 882604


async def _pool():
    return await asyncpg.create_pool(_DSN, min_size=1, max_size=3,
                                     server_settings={"lock_timeout": "3000"})


async def _clean(pool, cid=None):
    await pool.execute("DELETE FROM virtual_state_history WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM virtual_states WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM contact_crm WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)


async def _contact(pool, name: str) -> uuid.UUID:
    return await pool.fetchval(
        "INSERT INTO unified_contacts(owner_id, first_name) "
        "VALUES($1,$2) RETURNING id", OWNER, name)


@pytest.mark.asyncio
async def test_the_buyer_leaves_the_chase_list():
    from services import virtual_layer as V
    from services.contacts_hub import crm_engine

    pool = await _pool()
    try:
        await _clean(pool)
        buyer = await _contact(pool, "Купил")
        still = await _contact(pool, "Думает")

        # Оба дошли до «готов купить».
        for cid in (buyer, still):
            await V.signal(pool, OWNER, V.USER, cid, "asked_how_to_pay",
                           confidence=0.7, source="intent_sensor")
        ov = await V.overview(pool, OWNER)
        assert {str(buyer), str(still)} <= {h["entity_id"] for h in ov["hot"]}

        # Владелец ставит одному «Выиграно» на экране контакта.
        await crm_engine.upsert_crm(pool, OWNER, buyer, {"stage": "won"})

        st = await V.get_state(pool, OWNER, V.USER, buyer)
        assert st and st["value"] == "purchased", (
            f"слой так и не узнал о продаже: {st}")
        assert st["expires_at"] is None, "«Купил» не должен распадаться"

        ov = await V.overview(pool, OWNER)
        chase = {h["entity_id"] for h in ov["hot"]}
        assert str(buyer) not in chase, (
            "купивший остался в списке «кого дожимать первыми»")
        assert str(still) in chase, "дожимать как раз этого и надо"
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_the_purchase_is_recorded_in_the_history():
    """Историю переходов читает экран контакта и рисунок «заходил и уходил»."""
    from services import virtual_layer as V
    from services.contacts_hub import crm_engine

    pool = await _pool()
    try:
        await _clean(pool)
        cid = await _contact(pool, "Купил")
        await V.signal(pool, OWNER, V.USER, cid, "asked_price", source="intent_sensor")
        await crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "won"})
        rows = await pool.fetch(
            "SELECT from_value, to_value, reason FROM virtual_state_history "
            "WHERE owner_id=$1 AND entity_id=$2 ORDER BY created_at",
            OWNER, str(cid))
        assert any(r["to_value"] == "purchased" for r in rows), rows
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_a_lost_contact_who_comes_back_and_pays():
    from services import virtual_layer as V
    from services.contacts_hub import crm_engine

    pool = await _pool()
    try:
        await _clean(pool)
        cid = await _contact(pool, "Вернулся")
        await crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "lost"})
        st = await V.get_state(pool, OWNER, V.USER, cid)
        assert st and st["value"] == V.LOST, st
        await crm_engine.upsert_crm(pool, OWNER, cid, {"stage": "won"})
        st = await V.get_state(pool, OWNER, V.USER, cid)
        assert st["value"] == "purchased", (
            "человека записали в потерянные, он вернулся и купил, а слой "
            f"держит его потерянным: {st}")
    finally:
        await _clean(pool)
        await pool.close()
