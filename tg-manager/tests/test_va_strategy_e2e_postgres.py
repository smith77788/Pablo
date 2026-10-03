"""Реальная БД: стратегия изолирована по владельцу и не теряет конкурентные правки."""
import asyncio
import os
from pathlib import Path

import pytest

from services import va_strategy as vs

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="Нужен тестовый PostgreSQL")


def test_strategy_roundtrip_owner_isolation_and_stale_save():
    import asyncpg

    async def run():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        owner = 990778112
        try:
            # Только новая таблица; миграция образцов проверяется общим набором схем.
            sql = (Path(__file__).resolve().parents[1] / "schema_v235_va_network_strategy.sql").read_text(encoding="utf-8")
            await pool.execute(sql.split("ALTER TABLE")[0])
            await pool.execute("DELETE FROM va_network_strategy WHERE owner_id=$1", owner)
            initial = await vs.get_strategy(pool, owner)
            assert initial["revision"] == 0 and initial["settings"]["enabled"] is False
            settings = vs.validate({"enabled": True, "destination": "@main_resource",
                                    "action": "Подписаться", "business": {"facts": "Проверенный факт"}})
            saved = await vs.save_strategy(pool, owner, {"revision": 0, "settings": settings})
            assert await vs.get_strategy(pool, owner) == saved
            assert (await vs.get_strategy(pool, owner + 1))["revision"] == 0
            results = await asyncio.gather(*(
                vs.save_strategy(pool, owner, {"revision": 1, "settings": {**settings, "name": name}})
                for name in ("Первая правка", "Вторая правка")), return_exceptions=True)
            assert sum(isinstance(x, vs.StrategyError) for x in results) == 1
            assert (await vs.get_strategy(pool, owner))["revision"] == 2
        finally:
            await pool.execute("DELETE FROM va_network_strategy WHERE owner_id=$1", owner)
            await pool.close()

    asyncio.run(run())
