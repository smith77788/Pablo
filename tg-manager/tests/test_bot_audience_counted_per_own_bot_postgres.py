"""Аудитория ботов считается по СВОИМ ботам, а не по всей таблице платформы.

Меню ботов (`db.get_bots` зовут больше сорока экранов), сеть, кластеры,
здоровье ботов и реестр активов присоединяли
`(SELECT bot_id, COUNT(*) FROM bot_users WHERE is_active GROUP BY bot_id)` —
то есть на КАЖДОЕ открытие меню Postgres пересчитывал подписчиков всех ботов
всех владельцев платформы, чтобы взять из них десяток своих. Время меню росло
с размером платформы, а не с флотом владельца: на стенде с 1,5 млн чужих
подписчиков `get_bots` владельца с десятью ботами шёл 60–90 мс, после — 3–4.

Счёт теперь берётся LATERAL по каждому своему боту (индекс по bot_id).
`NULLIF(COUNT(*), 0)` сохраняет прежнюю форму: у бота без подписчиков счёт
NULL, как у LEFT JOIN без строки, поэтому `ORDER BY ... DESC` (NULL первыми)
и `COALESCE(..., 0)` ведут себя ровно как раньше.

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/<база>.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_844
OTHER = 990_845
B0 = 990_844_000
ROOT = Path(__file__).resolve().parent.parent

# бот → (активных подписчиков, неактивных, активных автоответов)
_BOTS = {B0 + 1: (5, 2, 1), B0 + 2: (0, 3, 0), B0 + 3: (12, 0, 2)}
_FOREIGN = B0 + 9          # чужой бот: его подписчики не должны попасть никуда


def _go(body):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _main():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            await _clean(p)
            await _seed(p)
            await body(p)
        finally:
            await _clean(p)
            await p.close()

    asyncio.run(_main())


async def _clean(p):
    ids = list(_BOTS) + [_FOREIGN]
    await p.execute("DELETE FROM auto_replies WHERE bot_id = ANY($1::bigint[])", ids)
    await p.execute("DELETE FROM bot_users WHERE bot_id = ANY($1::bigint[])", ids)
    await p.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", ids)


async def _seed(p):
    for i, (bid, (act, inact, ar)) in enumerate(_BOTS.items()):
        await p.execute(
            "INSERT INTO managed_bots (token, bot_id, username, added_by, cluster, swarm_enabled) "
            "VALUES ($1, $2, $3, $4, 'c1', TRUE)", f"t-{bid}", bid, f"aud_{i}", OWNER)
        await p.execute(
            "INSERT INTO bot_users (bot_id, user_id, is_active) "
            "SELECT $1, g, g <= $2 FROM generate_series(1, $3) g", bid, act, act + inact)
        for k in range(ar):
            await p.execute(
                "INSERT INTO auto_replies (bot_id, trigger_type, keyword, response_text, is_active) "
                "VALUES ($1, 'keyword', $2, 'ok', TRUE)", bid, f"kw{k}")
    await p.execute(
        "INSERT INTO managed_bots (token, bot_id, username, added_by, cluster) "
        "VALUES ('t-foreign', $1, 'foreign', $2, 'c1')", _FOREIGN, OTHER)
    await p.execute(
        "INSERT INTO bot_users (bot_id, user_id, is_active) "
        "SELECT $1, g, TRUE FROM generate_series(1, 50) g", _FOREIGN)


def test_counts_are_per_own_bot_and_keep_their_old_shape():
    from database import db

    async def body(pool):
        bots = {b["bot_id"]: b for b in await db.get_bots(pool, OWNER)}
        assert set(bots) == set(_BOTS)
        for bid, (act, _i, ar) in _BOTS.items():
            assert bots[bid]["audience_count"] == act
            assert bots[bid]["active_replies_count"] == ar

        clusters = await db.get_cluster_list(pool, OWNER)
        assert [(c["cluster"], c["bot_count"], c["total_audience"]) for c in clusters] == [
            ("c1", 3, 17)]

        in_cluster = {r["bot_id"]: r["audience_count"]
                      for r in await db.get_bots_in_cluster(pool, OWNER, "c1")}
        assert in_cluster == {bid: v[0] for bid, v in _BOTS.items()}

        ranking = [r["bot_id"] for r in await db.get_bot_ranking(pool, OWNER)]
        assert ranking[0] == B0 + 3

        # Бот без подписчиков — NULL, то есть первым в DESC, как и было.
        health = await db.get_network_health(pool, OWNER)
        assert [h["bot_id"] for h in health] == [B0 + 2, B0 + 3, B0 + 1]
        assert health[0]["audience"] == 0

    _go(body)


_WHOLE_TABLE = re.compile(
    r"LEFT JOIN \(\s*SELECT bot_id, COUNT\(\*\) AS \w+\s+FROM (bot_users|auto_replies)"
    r"\s+WHERE is_active\s*=\s*TRUE\s+GROUP BY bot_id", re.I)


def test_no_menu_counts_the_whole_platform():
    hits = []
    for sub in ("database", "bot", "services"):
        for path in (ROOT / sub).rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            for m in _WHOLE_TABLE.finditer(src):
                hits.append(f"{path.relative_to(ROOT)}:{src[:m.start()].count(chr(10)) + 1}")
    assert not hits, "подписчики всей платформы считаются ради своих ботов: " + ", ".join(hits)


def test_detector_sees_the_old_form():
    old = """LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users
               WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id"""
    assert _WHOLE_TABLE.search(old)
