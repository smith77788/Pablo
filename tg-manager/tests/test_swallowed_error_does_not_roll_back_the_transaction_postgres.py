"""Ошибка, «проглоченная» внутри транзакции, не откатывает её молча.

В Postgres упавший запрос ломает ВСЮ транзакцию, а COMMIT такой транзакции
не бросает, а молча становится ROLLBACK. Поэтому `try/except: pass` вокруг
запроса внутри `conn.transaction()` — это не «не критично», а тихий откат
всего, что транзакция успела сделать.

Случаи, которые здесь стерегутся:
  * `db.record_deep_link_visit`: повторный переход по ссылке ловил
    UniqueViolationError — и вместе с ним откатывался `click_count + 1`.
    Счётчик переходов считал только новых людей;
  * `db.subscribe_to_funnel`: сбой счётчика «вошли в воронку» откатывал саму
    подписку человека (точка сохранения).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

BOT_ID = 880077991
OWNER = 990418


def test_repeat_visit_still_counts_a_click():
    import asyncpg

    from database import db

    async def go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:100]}")
        try:
            try:
                await pool.fetchval("SELECT 1 FROM bot_deep_links LIMIT 1")
            except Exception as exc:
                pytest.skip(f"схема не накатана: {str(exc)[:80]}")
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
            await pool.execute(
                "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
                "VALUES($1,$2,'deeplink_tx_test_bot',TRUE,$3)",
                f"1:dl-{BOT_ID}", BOT_ID, OWNER)
            link = await db.create_deep_link(pool, BOT_ID, "Реклама", "ad1")
            for _ in range(3):
                await db.record_deep_link_visit(pool, BOT_ID, "ad1", 555)
            await db.record_deep_link_visit(pool, BOT_ID, "ad1", 556)
            row = await pool.fetchrow(
                "SELECT click_count, unique_users FROM bot_deep_links WHERE id=$1", link)
            assert (row["click_count"], row["unique_users"]) == (4, 2), dict(row)
        finally:
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
            await pool.close()

    asyncio.run(go())
