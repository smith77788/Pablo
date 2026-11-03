"""Счёт ключевых слов — один запрос на сообщение, и он верно считает.

`db.record_message_keywords` зовётся на КАЖДОЕ входящее сообщение каждого
подключённого бота. Раньше это было до десяти запросов подряд (по слову), то
есть десять захватов соединения из пула на сообщение. Проверяем: (1) запрос
один, (2) на настоящей схеме счёт растёт верно, повтор слова в сообщении не
считается дважды, (3) одновременные сообщения одного бота не взаимоблокируются
(слова идут в запрос в одном порядке).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from database import db

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
BOT_ID = 9_917_862_001


class _CountingPool:
    def __init__(self):
        self.calls = []

    async def execute(self, q, *args):
        self.calls.append((q, args))


async def test_one_query_per_message_with_sorted_first_words():
    pool = _CountingPool()
    text = " ".join(f"слово{chr(ord('а') + i)}" for i in range(15)) + " Привет привет"
    await db.record_message_keywords(pool, 1, text)
    assert len(pool.calls) == 1
    words = pool.calls[0][1][1]
    assert len(words) == 10
    assert words == sorted(words)
    assert "словоа" in words              # первые по тексту, а не случайные


async def test_no_query_without_words():
    pool = _CountingPool()
    await db.record_message_keywords(pool, 1, "12 ok !!")
    await db.record_message_keywords(pool, 1, "")
    assert pool.calls == []


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
async def test_counts_on_real_schema_and_concurrent_messages_do_not_deadlock():
    import asyncpg

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8)
    try:
        await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
        await pool.execute(
            "INSERT INTO managed_bots(bot_id, token, added_by) VALUES($1, 'x', 1)",
            BOT_ID)

        await db.record_message_keywords(pool, BOT_ID, "Привет мир, привет!")
        await db.record_message_keywords(pool, BOT_ID, "мир да")

        rows = await pool.fetch(
            "SELECT keyword, count FROM keyword_stats WHERE bot_id=$1", BOT_ID)
        got = {r["keyword"]: r["count"] for r in rows}
        assert got == {"привет": 1, "мир": 2}

        # Одни и те же слова в разном порядке, параллельно: при несортированном
        # порядке блокировок это даёт DeadlockDetected (глушится в debug —
        # то есть счёт молча теряется).
        a = "альфа бета гамма дельта эпсилон"
        b = "эпсилон дельта гамма бета альфа"
        await asyncio.gather(*[
            db.record_message_keywords(pool, BOT_ID, a if i % 2 else b)
            for i in range(40)])
        n = await pool.fetchval(
            "SELECT count FROM keyword_stats WHERE bot_id=$1 AND keyword='альфа'",
            BOT_ID)
        assert n == 40
    finally:
        await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
        await pool.close()
