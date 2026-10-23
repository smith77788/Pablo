"""Каскад считает по агрегату — и на живой базе отвечает то же, что раньше.

ЧТО БЫЛО. `recompute_cascade` и `recompute_bot_cascade` читали ВСЕ состояния
людей владельца строками: по одной на человека, на каждом сердцебиении (15
минут), и дважды — каскад аудитории и каскад ботов. Пока в слой попадали только
контакты из адресной книги, это было терпимо; с подписчиками бота число людей в
слое растёт вместе с аудиторией, а каскаду нужны ровно два числа: сколько всего
и сколько горячих.

ЧТО ТЕПЕРЬ. Запрос группирует в базе («значение + просрочено ли + сколько»),
решение считается по счётчикам. Здесь проверяется то, что заглушкой пула
проверить нельзя: что группировка и приведение `expires_at` к признаку
просрочки на живом Postgres дают тот же вердикт, что подсчёт по строкам.

Без INFRAGRAM_TEST_DSN — скип.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 330977


async def _seed(conn, rows):
    """rows: (entity_id, value, source, expires_in_hours|None)."""
    for entity_id, value, source, hours in rows:
        await conn.execute(
            "INSERT INTO virtual_states(owner_id, entity_type, entity_id, "
            "state_key, value, confidence, source, expires_at) "
            "VALUES($1,'user',$2,'funnel',$3,0.8,$4, "
            "       CASE WHEN $5::float IS NULL THEN NULL "
            "            ELSE now() + ($5::float * interval '1 hour') END)",
            OWNER, str(entity_id), value, source, hours)


async def _cleanup(conn):
    await conn.execute("DELETE FROM virtual_state_history WHERE owner_id=$1", OWNER)
    await conn.execute("DELETE FROM virtual_states WHERE owner_id=$1", OWNER)


@pytest.mark.asyncio
async def test_audience_cascade_matches_the_row_by_row_answer():
    from services import virtual_layer as V

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        # 30 свежих «готов», 10 просроченных «готов» (они уже не горячие),
        # 60 «новых». Горячих 30 из 100 — выше порога и по числу, и по доле.
        rows = ([(f"u{i}", "ready", "bot_5", 5.0) for i in range(30)]
                + [(f"e{i}", "ready", "bot_5", -5.0) for i in range(10)]
                + [(f"n{i}", "new", "bot_5", None) for i in range(60)])
        await _seed(conn, rows)

        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
        asked: list[int] = []
        inner_fetch = pool.fetch

        class _Counting:
            def __init__(self, inner):
                self._inner = inner

            async def fetch(self, query, *args, **kw):
                out = await inner_fetch(query, *args, **kw)
                if "virtual_states" in query:
                    asked.append(len(out))
                return out

            def __getattr__(self, name):
                return getattr(self._inner, name)

        try:
            verdict = await V.recompute_cascade(
                _Counting(pool), OWNER, V.NETWORK, "audience", V.USER,
                min_count=20, min_share=0.15)
            assert verdict == "hot", verdict
            assert asked and max(asked) <= 6, (
                f"каскад прочитал {asked} строк на сто детей — не агрегат")

            stored = await conn.fetchval(
                "SELECT value FROM virtual_states WHERE owner_id=$1 "
                "AND entity_type='network' AND entity_id='audience'", OWNER)
            assert stored == "hot"

            # Те же тридцать, но просроченные: аудитория горячей быть не должна.
            await conn.execute(
                "UPDATE virtual_states SET expires_at = now() - interval '1 hour' "
                "WHERE owner_id=$1 AND value='ready'", OWNER)
            verdict = await V.recompute_cascade(
                _Counting(pool), OWNER, V.NETWORK, "audience", V.USER,
                min_count=20, min_share=0.15)
            assert verdict == "cold", (
                f"просроченные «готов» всё ещё греют аудиторию: {verdict}")
        finally:
            await pool.close()
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_bot_cascade_groups_by_source_on_live_data():
    from services import virtual_layer as V

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        # Бот 10: шесть «готов» из десяти. Бот 20: один из десяти.
        rows = ([(f"a{i}", "ready", "bot_10", 5.0) for i in range(6)]
                + [(f"b{i}", "curious", "bot_10", 100.0) for i in range(4)]
                + [("c0", "ready", "bot_20", 5.0)]
                + [(f"d{i}", "curious", "bot_20", 100.0) for i in range(9)])
        await _seed(conn, rows)

        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
        try:
            out = await V.recompute_bot_cascade(pool, OWNER)
            assert out.get("10") == "hot", out
            assert out.get("20") != "hot", out
            stored = await conn.fetchval(
                "SELECT value FROM virtual_states WHERE owner_id=$1 "
                "AND entity_type='bot' AND entity_id='10'", OWNER)
            assert stored == "hot"
        finally:
            await pool.close()
    finally:
        await _cleanup(conn)
        await conn.close()
