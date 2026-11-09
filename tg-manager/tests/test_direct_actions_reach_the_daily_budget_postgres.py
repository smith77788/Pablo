"""Действие мимо очереди операций уменьшает суточный лимит аккаунта (живая база)."""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна живая база INFRAGRAM_TEST_DSN")

OWNER, ACC = 990_071_001, 990_071_002


def test_a_join_made_outside_the_queue_is_seen_by_the_budget():
    import asyncpg
    from services import account_budget, account_gate, account_manager, pacing_store

    class JoinChannelRequest:
        pass

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        saved_t = account_manager._ACC_TRANSPORT.get(ACC)
        saved_q = dict(account_gate._direct_unsaved)
        account_gate._direct_unsaved.clear()
        try:
            await pool.execute("DELETE FROM operation_audit WHERE account_id=$1", ACC)
            before = await account_budget.actions_today(pool, ACC)
            for _ in range(3):
                account_gate.note_direct_action(ACC, JoinChannelRequest())
            # Владелец ещё неизвестен — действие ждёт, а не теряется.
            account_manager._ACC_TRANSPORT.pop(ACC, None)
            assert await pacing_store.flush_direct_actions(pool) == 0
            account_manager._ACC_TRANSPORT[ACC] = {"owner_id": OWNER}
            assert await pacing_store.flush_direct_actions(pool) == 1
            after = await account_budget.actions_today(pool, ACC)
            again = await pacing_store.flush_direct_actions(pool)
            return before, after, again
        finally:
            await pool.execute("DELETE FROM operation_audit WHERE account_id=$1", ACC)
            account_manager._ACC_TRANSPORT.pop(ACC, None)
            if saved_t is not None:
                account_manager._ACC_TRANSPORT[ACC] = saved_t
            account_gate._direct_unsaved.clear()
            account_gate._direct_unsaved.update(saved_q)
            await pool.close()

    before, after, again = asyncio.run(go())
    assert after - before == 3
    assert again == 0, "повторная запись удвоила бы счёт"
