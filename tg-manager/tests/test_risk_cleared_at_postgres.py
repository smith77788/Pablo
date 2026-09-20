"""Ручной сброс риск-карантина: risk_cleared_at реально снимает карантин.

Жалоба владельца: «кулдаун не сбрасывается на рисковых аккаунтах». Кнопка
«Сбросить кулдаун» чистила только cooldown_until, а is_account_quarantined
держал аккаунт вне операций по старым restriction_events. Теперь reset ставит
risk_cleared_at=NOW(), и карантин учитывает только ограничения ПОЗЖЕ этой отметки.

Живой Postgres (заглушка пула не проверяет JOIN/типы). Без INFRAGRAM_TEST_DSN —
скип. Инструкция по стенду — в tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")


async def _mk_account(conn, owner_id: int) -> int:
    return await conn.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
        "VALUES($1, $2, 'sess', TRUE) RETURNING id",
        owner_id, f"+7999{owner_id:07d}")


@pytest.mark.asyncio
async def test_risk_cleared_at_lifts_quarantine():
    from services.infra_memory import is_account_quarantined

    conn = await asyncpg.connect(_DSN)
    try:
        owner = 918273645
        acc_id = await _mk_account(conn, owner)
        try:
            # свежее серьёзное ограничение → карантин
            await conn.execute(
                "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
                "VALUES($1, $2, 'ban_detected', 'critical')", owner, acc_id)
            pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
            try:
                assert await is_account_quarantined(pool, acc_id) is True, (
                    "свежее критическое ограничение обязано давать карантин")

                # владелец снял риск — карантин уходит (событие СТАРШЕ отметки)
                await conn.execute(
                    "UPDATE tg_accounts SET cooldown_until=NULL, risk_cleared_at=NOW() WHERE id=$1",
                    acc_id)
                assert await is_account_quarantined(pool, acc_id) is False, (
                    "после ручного сброса риска карантина быть не должно")

                # НОВОЕ ограничение уже ПОСЛЕ сброса → снова карантин (защита жива)
                await conn.execute(
                    "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
                    "VALUES($1, $2, 'ban_detected', 'critical')", owner, acc_id)
                assert await is_account_quarantined(pool, acc_id) is True, (
                    "свежее ограничение после сброса обязано снова уводить в карантин")
            finally:
                await pool.close()
        finally:
            await conn.execute("DELETE FROM restriction_events WHERE account_id=$1", acc_id)
            await conn.execute("DELETE FROM tg_accounts WHERE id=$1", acc_id)
    finally:
        await conn.close()
