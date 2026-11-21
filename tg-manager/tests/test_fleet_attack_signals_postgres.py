"""Сигналы атаки на флот — запросы на живой базе (schema_v378).

Заглушка пула не проверяет ни типы параметров, ни то, что колонка есть:
отметки разбора, журнал безопасности и сводка «Защиты» проверяются здесь.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import account_alarm as al
from services import account_shield as ash

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995781
NOW = 1_900_000_000

pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres (INFRAGRAM_TEST_DSN)")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _pool():
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    await pool.execute("DELETE FROM account_security_events WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER)
    acc_id = await pool.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, username) "
        "VALUES($1, '+79990001122', 'x', TRUE, 'acc_alarm') RETURNING id", OWNER)
    return pool, int(acc_id)


def test_marks_events_and_the_shield_screen_read_back():
    async def _go():
        pool, acc_id = await _pool()
        try:
            acc = {"id": acc_id, "owner_id": OWNER, "username": "acc_alarm"}
            found = {"events": [{"kind": al.KIND_RESET_REQUESTED, "until": NOW},
                                {"kind": al.KIND_LOGIN_ATTEMPT, "at": NOW - 60, "msg_id": 41}],
                     "seen_id": 41, "declined": False, "mismatch": True,
                     "email_fp": "abc", "twofa_on": True, "pw_read": True}
            assert await al.record(pool, acc, found, bot=None) == 2
            row = await pool.fetchrow(
                "SELECT service_seen_msg_id, twofa_reset_until, twofa_mismatch_at, "
                "recovery_email_fp FROM tg_accounts WHERE id=$1", acc_id)
            assert row["service_seen_msg_id"] == 41
            assert int(row["twofa_reset_until"].timestamp()) == NOW
            assert row["twofa_mismatch_at"] is not None and row["recovery_email_fp"] == "abc"

            # Проход без пароля (не прочитался): отметки не стираются.
            await al.record(pool, acc, {"events": [], "seen_id": None}, bot=None)
            row = await pool.fetchrow(
                "SELECT service_seen_msg_id, twofa_reset_until, twofa_mismatch_at "
                "FROM tg_accounts WHERE id=$1", acc_id)
            assert row["service_seen_msg_id"] == 41 and row["twofa_reset_until"] is not None
            assert row["twofa_mismatch_at"] is not None

            evs = await al.recent_events(pool, OWNER)
            assert {e["kind"] for e in evs} == {al.KIND_RESET_REQUESTED, al.KIND_LOGIN_ATTEMPT}
            assert all(e["account"] == "@acc_alarm" for e in evs)
            assert await al.recent_events(pool, OWNER, acc_id) and not await al.recent_events(
                pool, OWNER, acc_id + 100000)

            await al.note_code_read(pool, OWNER, acc_id)
            assert await pool.fetchval(
                "SELECT login_code_read_at IS NOT NULL FROM tg_accounts WHERE id=$1", acc_id)
        finally:
            await pool.execute("DELETE FROM account_security_events WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()
    _run(_go())


def test_password_guard_is_on_by_default_and_can_be_turned_off():
    async def _go():
        pool, _ = await _pool()
        try:
            cfg = await ash.get_shield_config(pool, OWNER)
            assert cfg.guard_twofa is True and cfg.guard_reset is True
            # Строка, заведённая другой настройкой, тоже получает защиту.
            await pool.execute("INSERT INTO shield_configs(owner_id, notify_admin) VALUES($1, TRUE)",
                               OWNER)
            cfg = await ash.get_shield_config(pool, OWNER)
            assert cfg.guard_twofa is True and cfg.guard_reset is True
            cfg = await ash.save_shield_config(pool, OWNER, guard_reset=False)
            assert cfg.guard_reset is False and cfg.as_dict()["guard_reset"] is False
        finally:
            await pool.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()
    _run(_go())
