"""Счётчики дашборда — один запрос и одно соединение, а не семь.

Поток событий зовёт _stats каждые 15 секунд на КАЖДОЕ открытое приложение.
Семь round-trip подряд — семь ожиданий сети; семь параллельных — семь
соединений из пула (по умолчанию 20) на одного пользователя. Проверяем на
живом Postgres, что объединённый запрос действительно считает то же самое.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995001
OTHER = 995002
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


class _Counting:
    """Считает запросы и пик одновременных соединений."""

    def __init__(self, inner):
        self._inner = inner
        self.queries = 0
        self.peak = 0
        self._live = 0

    async def _wrap(self, coro):
        self.queries += 1
        self._live += 1
        self.peak = max(self.peak, self._live)
        try:
            return await coro
        finally:
            self._live -= 1

    async def fetchrow(self, q, *a):
        return await self._wrap(self._inner.fetchrow(q, *a))

    async def fetchval(self, q, *a):
        return await self._wrap(self._inner.fetchval(q, *a))

    async def fetch(self, q, *a):
        return await self._wrap(self._inner.fetch(q, *a))

    async def execute(self, q, *a):
        return await self._wrap(self._inner.execute(q, *a))


def _seed(pool):
    async def _s():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                           [OWNER, OTHER])
        await pool.execute("DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])",
                           [OWNER, OTHER])
        await pool.execute("DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                           [OWNER, OTHER])
        for i in range(3):
            await pool.execute(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
                "VALUES($1,$2,$3,TRUE,'active')", OWNER, f"+7995001{i:06d}", f"s{i}")
        await pool.execute(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
            "VALUES($1,$2,$3,TRUE,'active')", OTHER, "+7995002000000", "x")
        for i in range(2):
            await pool.execute(
                "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title) "
                "VALUES($1,$2,$3,$4)", OWNER, 1, 900000 + i, f"Канал {i}")
        await pool.execute(
            "INSERT INTO operation_queue(owner_id, op_type, status, params) "
            "VALUES($1,'mass_invite','running','{}'::jsonb)", OWNER)
    return _run(_s())


def test_stats_is_one_query_and_counts_owner_scope(pool):
    from services import mini_app_api as m

    _seed(pool)
    counting = _Counting(pool)
    res = _run(m._stats(counting, OWNER))

    assert counting.queries == 1, "счётчики должны считаться одним запросом"
    assert counting.peak == 1, "и одним соединением из пула"
    assert res["accounts"] == 3, "чужой аккаунт попал в счётчик"
    assert res["channels"] == 2
    assert res["ops_running"] == 1
    assert res["bots"] == 0 and res["subscribers"] == 0
    assert set(res) == set(m._STATS_KEYS)


def test_admin_scope_counts_the_whole_platform(pool):
    from services import mini_app_api as m

    _seed(pool)
    own = _run(m._stats(pool, OWNER))
    everyone = _run(m._stats(pool, OWNER, admin=True))
    assert everyone["accounts"] > own["accounts"], (
        "межтенантный счётчик должен видеть и чужие аккаунты")
    assert everyone["channels"] == own["channels"], (
        "остальные счётчики остаются по владельцу")


def test_fallback_counts_one_by_one_when_combined_query_fails(pool):
    """Сломанный объединённый запрос не должен обнулять весь дашборд."""
    from services import mini_app_api as m

    _seed(pool)

    class _NoCombined(_Counting):
        async def fetchrow(self, q, *a):
            raise RuntimeError("объединённый запрос недоступен")

    counting = _NoCombined(pool)
    res = _run(m._stats(counting, OWNER))
    assert counting.queries == len(m._STATS_KEYS), "должен быть запрос на счётчик"
    assert res["accounts"] == 3 and res["channels"] == 2


def test_fallback_keeps_admin_scope(pool):
    """В запасном пути межтенантный счётчик не принимает $1 — его нельзя слать."""
    from services import mini_app_api as m

    _seed(pool)

    class _NoCombined(_Counting):
        async def fetchrow(self, q, *a):
            raise RuntimeError("нет")

    res = _run(m._stats(_NoCombined(pool), OWNER, admin=True))
    assert res["accounts"] >= 4, "лишний параметр обнулил бы счётчик"
