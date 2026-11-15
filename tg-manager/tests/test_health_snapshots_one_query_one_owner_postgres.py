"""Снимки здоровья пишутся одним запросом и только по нужному владельцу.

ЧТО БЫЛО (замер на стенде 12.11.2026: 6000 аккаунтов, 300 владельцев).
`_persist_health_snapshots` делала `pool.execute` на КАЖДЫЙ аккаунт кеша —
6000 захватов соединения подряд; очередь к пулу доходила до 28 ждущих, и
защитная запись журнала целей операций ждала пул до 0.5 с. А проверка
аккаунтов ОДНОГО владельца звала её без фильтра: кеш общий на процесс, и
каждая такая проверка переписывала историю всего флота платформы (96 тыс.
строк за минуту на стенде).

ЧТО ПРОВЕРЯЕМ на живой базе: с owner_id пишутся строки только этого
владельца; без него — все аккаунты кеша; и в обоих случаях запись идёт ОДНИМ
INSERT, а не строкой на аккаунт.
"""
from __future__ import annotations

import asyncio
import os
import random

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

A = 991_000_000 + random.randint(1, 99_999)
B = A + 100_000


def test_snapshots_batch_and_owner_scope():
    from services import account_health as ah

    async def go():
        import asyncpg

        conn = await asyncpg.connect(DSN)
        inserts: list[str] = []

        class _P:
            async def fetch(self, q, *a):
                return await conn.fetch(q, *a)

            async def execute(self, q, *a):
                if "INSERT INTO account_health_history" in q:
                    inserts.append(q)
                return await conn.execute(q, *a)

        saved = dict(ah._health_cache)
        try:
            ids = {A: [], B: []}
            for owner in (A, B):
                for i in range(30):
                    ids[owner].append(await conn.fetchval(
                        "INSERT INTO tg_accounts(owner_id, phone, session_str, trust_score)"
                        " VALUES($1,$2,'s',0.8) RETURNING id",
                        owner, f"+7{owner}{i:03d}"))
            ah._health_cache.clear()
            for aid in ids[A] + ids[B]:
                ah.get_health(aid)

            n_a = await ah._persist_health_snapshots(_P(), A)
            rows_a = await conn.fetch(
                "SELECT owner_id, COUNT(*) c FROM account_health_history"
                " WHERE owner_id = ANY($1::bigint[]) GROUP BY owner_id", [A, B])
            inserts_a = len(inserts)

            n_all = await ah._persist_health_snapshots(_P())
            rows_all = await conn.fetch(
                "SELECT owner_id, COUNT(*) c FROM account_health_history"
                " WHERE owner_id = ANY($1::bigint[]) GROUP BY owner_id", [A, B])
            return n_a, {r["owner_id"]: r["c"] for r in rows_a}, inserts_a, \
                n_all, {r["owner_id"]: r["c"] for r in rows_all}, len(inserts)
        finally:
            ah._health_cache.clear()
            ah._health_cache.update(saved)
            await conn.execute(
                "DELETE FROM account_health_history WHERE owner_id = ANY($1::bigint[])", [A, B])
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])", [A, B])
            await conn.close()

    n_a, rows_a, inserts_a, n_all, rows_all, inserts_total = asyncio.run(go())
    assert n_a == 30 and rows_a == {A: 30}, (n_a, rows_a)
    assert inserts_a == 1, f"снимки владельца записаны {inserts_a} запросами вместо одного"
    assert n_all == 60 and rows_all == {A: 60, B: 30}, (n_all, rows_all)
    assert inserts_total == 2
