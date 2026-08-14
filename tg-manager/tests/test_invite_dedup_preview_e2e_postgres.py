"""Превью дедупа при загрузке списка: сколько уже приглашались в группу."""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 990901
GROUP = "@dedup_target"
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
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
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


def test_counts_already_invited(pool):
    from bot.handlers.mass_inviter import _count_already_invited
    _run(pool.execute("DELETE FROM invite_target_log WHERE owner_id=$1", OWNER))
    # @u1 и @u2 уже приглашались в эту группу
    for t in ("@u1", "@u2"):
        _run(pool.execute(
            "INSERT INTO invite_target_log(owner_id, group_key, target) VALUES($1,$2,$3) "
            "ON CONFLICT DO NOTHING", OWNER, GROUP, t))
    items = ["@u1", "@u2", "@u3", "123456"]
    already = _run(_count_already_invited(pool, OWNER, GROUP, items))
    assert already == 2                          # только @u1,@u2
    # другая группа — пересечения нет
    assert _run(_count_already_invited(pool, OWNER, "@other", items)) == 0
    _run(pool.execute("DELETE FROM invite_target_log WHERE owner_id=$1", OWNER))


def test_failopen_on_huge_list_or_empty(pool):
    from bot.handlers.mass_inviter import _count_already_invited
    assert _run(_count_already_invited(pool, OWNER, "", ["@a"])) == 0   # нет группы
    assert _run(_count_already_invited(pool, OWNER, GROUP, [])) == 0     # пусто
    big = [f"@u{i}" for i in range(50_001)]                              # больше порога
    assert _run(_count_already_invited(pool, OWNER, GROUP, big)) == 0
