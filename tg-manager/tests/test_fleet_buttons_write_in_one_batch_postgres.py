"""Кнопки «Определить возможности» и «Применить балансировку» пишут пачкой.

Обе делали запрос на аккаунт прямо в ответе на кнопку: на флоте в тысячи
аккаунтов владелец ждал секунды. Теперь одна запись на весь флот; сверяем
на живой базе, что записи те же и запись одна.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
OWNER = 995611


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Counting:
    def __init__(self, pool):
        self._p, self.writes = pool, []

    async def fetch(self, *a):
        return await self._p.fetch(*a)

    async def execute(self, q, *a):
        self.writes.append(q)
        return await self._p.execute(q, *a)

    async def executemany(self, q, rows):
        self.writes.append(q)
        return await self._p.executemany(q, rows)


def test_capabilities_written_in_one_batch():
    import asyncpg

    from bot.handlers import infra_analytics as ia

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM account_capabilities WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            for i, trust in enumerate((0.9, 0.45, 0.1)):
                await pool.execute(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, trust_score) "
                    "VALUES($1,$2,'s',TRUE,'active',$3)", OWNER, f"+7990100{i}", trust)
            cb = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=OWNER),
                                 message=SimpleNamespace(edit_text=AsyncMock()))
            cp = _Counting(pool)
            await ia.cb_discover_capabilities(cb, cp)
            rows = await pool.fetch(
                "SELECT c.can_invite, c.can_dm, c.daily_dm_limit FROM account_capabilities c "
                "JOIN tg_accounts a ON a.id=c.account_id WHERE c.owner_id=$1 "
                "ORDER BY a.trust_score DESC", OWNER)
            return cp.writes, [tuple(r) for r in rows], cb.message.edit_text.call_args
        finally:
            await pool.execute("DELETE FROM account_capabilities WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()

    writes, rows, shown = _run(_go())
    assert len(writes) == 1, writes
    assert rows == [(True, True, 50), (False, True, 20), (False, False, 5)]
    assert "<b>3</b>" in shown.args[0]
