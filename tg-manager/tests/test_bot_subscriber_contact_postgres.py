"""Живая проверка: сигнал подписчика бота доходит до состояния в базе.

Заглушка пула не проверяет ни типы связывания, ни то, что строка реально
появилась. А здесь цена ошибки ровно в этом: `unified_contacts.id` — uuid в
виде текста, `virtual_states.entity_id` — текст, `telegram_user_id` — bigint, и
любое расхождение означает, что сигнал «ушёл», но состояния нет.

Без INFRAGRAM_TEST_DSN — скип. Инструкция по стенду — в
tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 604517
TAPPER = 9100001          # тапнул по кнопке
STARTER = 9100002         # только нажал /start


async def _cleanup(conn):
    await conn.execute(
        "DELETE FROM virtual_state_history WHERE owner_id=$1", OWNER)
    await conn.execute("DELETE FROM virtual_states WHERE owner_id=$1", OWNER)
    await conn.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)


@pytest.mark.asyncio
async def test_a_button_tap_creates_the_contact_and_qualifies_it():
    from services import virtual_layer

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
        try:
            out = await virtual_layer.signal_for_telegram_user(
                pool, OWNER, TAPPER, "clicked_offer", confidence=0.7,
                source="bot_777",
                identity={"username": "tapper", "first_name": "Тапер"})
            assert out and out["value"] == "qualified", out

            contact_id = await conn.fetchval(
                "SELECT id FROM unified_contacts "
                "WHERE owner_id=$1 AND telegram_user_id=$2", OWNER, TAPPER)
            assert contact_id, "контакт подписчика бота не появился"

            row = await conn.fetchrow(
                "SELECT value, confidence, source, expires_at FROM virtual_states "
                "WHERE owner_id=$1 AND entity_type=$2 AND entity_id=$3 "
                "AND state_key='funnel'",
                OWNER, virtual_layer.USER, str(contact_id))
            assert row, "состояние не записалось — сигнал ушёл в пустоту"
            assert row["value"] == "qualified"
            assert row["source"] == "bot_777"
            assert row["expires_at"] is not None, (
                "у состояния нет срока — распад его никогда не остудит")

            # Повторный тап того же человека не плодит второй контакт.
            await virtual_layer.signal_for_telegram_user(
                pool, OWNER, TAPPER, "clicked_offer", confidence=0.7,
                source="bot_777", identity={"username": "tapper"})
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM unified_contacts "
                "WHERE owner_id=$1 AND telegram_user_id=$2", OWNER, TAPPER) == 1

            # «Открыл бота» контакт не заводит: подписчиков десятки тысяч.
            assert await virtual_layer.signal_for_telegram_user(
                pool, OWNER, STARTER, "opened", confidence=0.5,
                source="bot_777", identity={"username": "starter"}) is None
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM unified_contacts "
                "WHERE owner_id=$1 AND telegram_user_id=$2", OWNER, STARTER) == 0
        finally:
            await pool.close()
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_the_state_is_visible_on_the_screen_and_in_history():
    """Состояние обязано попасть и в сводку экрана, и в историю переходов."""
    from services import virtual_layer

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
        try:
            await virtual_layer.signal_for_telegram_user(
                pool, OWNER, TAPPER, "asked_how_to_pay", confidence=0.8,
                source="bot_777", identity={"first_name": "Тапер"})

            overview = await virtual_layer.overview(pool, OWNER)
            assert overview["total"] == 1, overview
            assert [item["value"] for item in overview["funnel"]] == ["ready"]
            assert [h["value"] for h in overview["hot"]] == ["ready"], (
                "человек, готовый платить, не попал в список «кого дожимать»")
            assert overview["hot"][0].get("name") == "Тапер", (
                "в списке нет имени — по нему нельзя работать")

            moves = await conn.fetch(
                "SELECT from_value, to_value FROM virtual_state_history "
                "WHERE owner_id=$1 ORDER BY id", OWNER)
            assert [(m["from_value"], m["to_value"]) for m in moves] == [
                (None, "ready")], moves
        finally:
            await pool.close()
    finally:
        await _cleanup(conn)
        await conn.close()
