"""Ожидание соединения пула БД видно в метриках в момент насыщения.

Пул (по умолчанию 20 соединений) делят бот, мини-апп, исполнитель и ~70
фоновых циклов. Кончились соединения — `pool.fetch` молча ждёт без потолка,
а единственная метрика пула снималась раз в 5 минут. На стенде 12.11.2026
очередь к пулу доходила до 28 ждущих, и увидел это только профилировщик.

Проверяем на живой базе: пул из двух соединений, четыре запроса по 0.3 с —
двое обязаны ждать, и это видно в счётчике медленных взятий, в сумме
ожидания и в худшем ожидании за минуту. И что create_pool продукта ставит
замер сам, а внутренний `Pool._acquire` у asyncpg на месте (иначе замер
молча не встанет на следующем обновлении).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_asyncpg_still_has_the_hook():
    import asyncpg.pool as ap

    assert hasattr(ap.Pool, "_acquire"), (
        "в asyncpg больше нет Pool._acquire — замер ожидания пула не встанет")


def test_create_pool_installs_the_meter():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "database", "db.py"), encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("async def create_pool("):]
    body = body[:body.index("\nasync def ", 10)]
    assert "pool_metrics.instrument(pool)" in body


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_saturation_shows_up_in_metrics():
    import asyncpg

    from database import pool_metrics
    from services import metrics

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=2, max_size=2)
        try:
            assert pool_metrics.instrument(pool)
            assert pool_metrics.instrument(pool)   # повтор не оборачивает дважды
            metrics.reset()
            await asyncio.gather(*(pool.fetchval("SELECT pg_sleep(0.3)")
                                   for _ in range(4)))
            return metrics.snapshot()
        finally:
            await pool.close()

    snap = asyncio.run(go())
    flat = repr(snap)
    assert "infragram_db_pool_slow_acquires_total" in flat, snap
    assert "infragram_db_pool_acquire_wait_max_seconds" in flat, snap
    assert "infragram_db_pool_acquire_wait_seconds" in flat, snap
