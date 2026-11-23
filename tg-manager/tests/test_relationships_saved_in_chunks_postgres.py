"""Пересчёт графа связей пишет связи кусками, а не запросом на связь.

30 000 контактов одного аккаунта дают ~200 000 связей: поштучно это ~25 с
и кнопка «Пересчитать» падала в таймаут; теперь ~5 с, разбор пар — в потоке
(задержка цикла 553 → 116 мс), а ручка отдаёт счёт, а не весь список.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
OWNER = 995711


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_writes_do_not_grow_with_the_graph():
    import asyncpg

    from services.contacts_hub import relationship_engine as rel

    async def _go(n):
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM contact_relationships WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            ids = [uuid.uuid4() for _ in range(n)]
            await pool.executemany(
                "INSERT INTO unified_contacts(id, owner_id, display_name, company) "
                "VALUES($1,$2,$3,'Ромашка')", [(i, OWNER, f"К{k}") for k, i in enumerate(ids)])
            writes = []

            class _P:
                async def fetch(self, *a):
                    return await pool.fetch(*a)

                async def execute(self, q, *a):
                    writes.append(q)
                    return await pool.execute(q, *a)

                async def executemany(self, q, rows):
                    writes.append(q)
                    return await pool.executemany(q, rows)

            res = await rel.compute_relationships(_P(), OWNER)
            stored = await pool.fetchval(
                "SELECT COUNT(*) FROM contact_relationships WHERE owner_id=$1", OWNER)
            return len(writes), res["count"], stored
        finally:
            await pool.execute("DELETE FROM contact_relationships WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            await pool.close()

    small = _run(_go(5))
    big = _run(_go(60))
    assert small == (1, 10, 10), small
    assert big == (1, 1770, 1770), big
