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


def test_a_spam_block_seen_at_the_door_reaches_the_database():
    import asyncpg
    from services import account_gate, flood_engine, pacing_store

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        saved_q = dict(account_gate._peer_flood_unsaved)
        saved_s = dict(account_gate._STATUS)
        saved_f = dict(flood_engine._flood_state)
        account_gate._peer_flood_unsaved.clear()
        try:
            await pool.execute("DELETE FROM tg_accounts WHERE id = ANY($1::bigint[])",
                               [ACC, ACC + 1])
            await pool.execute(
                """INSERT INTO tg_accounts(id, owner_id, phone, session_str, acc_status, status_reason)
                   VALUES ($1, $3, '+990071002', '', 'active', NULL),
                          ($2, $3, '+990071003', '', 'frozen', 'заморожен раньше')""",
                ACC, ACC + 1, OWNER)
            account_gate.note_peer_flood(ACC, "InviteToChannelRequest")
            account_gate.note_peer_flood(ACC + 1, "InviteToChannelRequest")
            assert await pacing_store.flush_peer_floods(pool) == 2
            rows = await pool.fetch(
                "SELECT id, acc_status, status_reason, "
                "       cooldown_until > now() + INTERVAL '47 hours' AS paused "
                "FROM tg_accounts WHERE id = ANY($1::bigint[]) ORDER BY id",
                [ACC, ACC + 1])
            return [dict(r) for r in rows], await pacing_store.flush_peer_floods(pool)
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE id = ANY($1::bigint[])",
                               [ACC, ACC + 1])
            for live, old in ((account_gate._peer_flood_unsaved, saved_q),
                              (account_gate._STATUS, saved_s),
                              (flood_engine._flood_state, saved_f)):
                live.clear()
                live.update(old)
            await pool.close()

    (fresh, frozen), again = asyncio.run(go())
    assert fresh["acc_status"] == "spamblock" and fresh["paused"]
    assert "PEER_FLOOD" in fresh["status_reason"]
    # Более тяжёлый статус и его причину дверь не затирает.
    assert (frozen["acc_status"], frozen["status_reason"]) == ("frozen", "заморожен раньше")
    assert again == 0


def test_a_ban_seen_at_the_door_switches_the_account_off_in_the_database():
    import asyncpg
    from services import account_gate, flood_engine, pacing_store

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        saved_q = dict(account_gate._ban_unsaved)
        saved_f = dict(flood_engine._flood_state)
        account_gate._ban_unsaved.clear()
        try:
            await pool.execute("DELETE FROM tg_accounts WHERE id = $1", ACC)
            await pool.execute(
                """INSERT INTO tg_accounts(id, owner_id, phone, session_str, acc_status)
                   VALUES ($1, $2, '+990071002', '', 'active')""", ACC, OWNER)
            account_gate.note_ban(ACC, "UserDeactivatedBanError")
            assert await pacing_store.flush_bans(pool) == 1
            row = await pool.fetchrow(
                "SELECT is_active, acc_status, status_reason FROM tg_accounts WHERE id=$1", ACC)
            return dict(row), await pacing_store.flush_bans(pool)
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE id = $1", ACC)
            account_gate._ban_unsaved.clear()
            account_gate._ban_unsaved.update(saved_q)
            flood_engine._flood_state.clear()
            flood_engine._flood_state.update(saved_f)
            await pool.close()

    row, again = asyncio.run(go())
    assert (row["is_active"], row["acc_status"]) == (False, "banned")
    assert "заблокирован" in row["status_reason"]
    assert again == 0
