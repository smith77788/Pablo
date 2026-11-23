"""Пересчёт поведенческих оценок не спрашивает базу на каждую сущность.

`_recompute_scores` считал разнообразие переходов отдельным запросом на
каждую сущность с переходами: на стенде 2000 сущностей — 2000 запросов и
475 мс; теперь счёт идёт в том же GROUP BY и пересчёт занимает 42 мс.
Сверяем, что оценка экосистемы та же, что дал бы прежний поштучный счёт.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
OWNER = 995412


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_diversity_counted_in_the_aggregate():
    import asyncpg

    from services import behavioral_engine as be

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM behavioral_events WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM entity_behavioral_score WHERE owner_id=$1", OWNER)
            # Сущность 1 — три вида переходов, сущность 2 — один вид.
            for ent, dests in ((1, ["a", "b", "c", "a"]), (2, ["a", "a"])):
                for d in dests:
                    await pool.execute(
                        "INSERT INTO behavioral_events(owner_id, entity_type, entity_id, "
                        "event_type, meta, occurred_at) VALUES($1,'contact',$2,'cross_nav',"
                        "$3::jsonb, now() - interval '1 hour')",
                        OWNER, ent, json.dumps({"to_type": d}))
            per_entity = []

            class _P:
                async def fetch(self, *a):
                    return await pool.fetch(*a)

                async def fetchval(self, *a):
                    per_entity.append(a[0])
                    return await pool.fetchval(*a)

                async def executemany(self, *a):
                    return await pool.executemany(*a)

                async def execute(self, *a):
                    return await pool.execute(*a)

            await be._recompute_scores(_P())
            got = {r["entity_id"]: float(r["ecosystem_score"]) for r in await pool.fetch(
                "SELECT entity_id, ecosystem_score FROM entity_behavioral_score WHERE owner_id=$1",
                OWNER)}
            return per_entity, got
        finally:
            await pool.execute("DELETE FROM behavioral_events WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM entity_behavioral_score WHERE owner_id=$1", OWNER)
            await pool.close()

    per_entity, got = _run(_go())
    assert not per_entity, f"запрос на сущность: {per_entity[:1]}"
    # 4 перехода × 20 × (0.8 + 3×0.15) = 100 (потолок); 2 × 20 × 0.95 = 38
    assert got[1] == pytest.approx(100.0)
    assert got[2] == pytest.approx(38.0)
