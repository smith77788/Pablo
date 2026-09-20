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


@pytest.mark.asyncio
async def test_risk_cleared_at_lifts_pulse_quarantine():
    """Пульс (его мержит список/деталь аккаунта в Mini App) тоже обязан отпускать.

    Хвост первой итерации: операции аккаунт уже брали (is_account_quarantined
    чинён), а get_account_health продолжал считать старые restriction_events и
    светил «Карантин» — для владельца это та же жалоба «не сбрасывается».
    """
    from services.infra_memory import get_account_health

    conn = await asyncpg.connect(_DSN)
    try:
        owner = 918273646
        acc_id = await _mk_account(conn, owner)
        try:
            await conn.execute(
                "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
                "VALUES($1, $2, 'ban_detected', 'critical')", owner, acc_id)
            pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
            try:
                def _status(res):
                    by_id = {a["account_id"]: a for a in res["accounts"]}
                    assert acc_id in by_id, "аккаунт обязан быть в пульсе владельца"
                    return by_id[acc_id]["status"]

                assert _status(await get_account_health(pool, owner)) == "quarantine", (
                    "свежее критическое ограничение обязано давать карантин в пульсе")

                await conn.execute(
                    "UPDATE tg_accounts SET cooldown_until=NULL, risk_cleared_at=NOW() "
                    "WHERE id=$1", acc_id)
                assert _status(await get_account_health(pool, owner)) == "healthy", (
                    "после ручного сброса пульс не должен держать «Карантин»")

                await conn.execute(
                    "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
                    "VALUES($1, $2, 'ban_detected', 'critical')", owner, acc_id)
                assert _status(await get_account_health(pool, owner)) == "quarantine", (
                    "ограничение ПОСЛЕ сброса обязано снова уводить в карантин: "
                    "сброс не должен ослеплять пульс навсегда")
            finally:
                await pool.close()
        finally:
            await conn.execute("DELETE FROM restriction_events WHERE account_id=$1", acc_id)
            await conn.execute("DELETE FROM tg_accounts WHERE id=$1", acc_id)
    finally:
        await conn.close()


# Прогоняем по живому Postgres ТОТ ЖЕ SQL, что уходит из кнопок: берём его из
# services/account_reset.py, чтобы тест не мог разойтись с кодом.
def _reset_sql() -> str:
    from services.account_reset import _CLEAR_STATUS_SQL
    return f"UPDATE tg_accounts SET {_CLEAR_STATUS_SQL} WHERE id=$1 AND owner_id=$2"


@pytest.mark.asyncio
async def test_reset_sql_clears_only_transient_cooldown():
    """acc_status='cooldown' снимается сразу; жёсткие статусы и конфликт сессии — нет.

    Пока acc_status оставался 'cooldown', account_health.load_from_db считал
    аккаунт спамблоком (suitability dm/invite = False) и get_sorted_accounts
    молча выкидывал его из подбора — до часового цикла монитора.
    """
    conn = await asyncpg.connect(_DSN)
    try:
        owner = 918273647
        cases = {}
        try:
            for name, status, conflict in (
                ("cooldown", "cooldown", None),
                ("banned", "banned", None),
                ("conflict", "cooldown", "NOW()"),
            ):
                acc_id = await _mk_account(conn, owner + len(cases))
                await conn.execute(
                    f"UPDATE tg_accounts SET owner_id=$1, acc_status=$2, "
                    f"status_reason='было', cooldown_until=NOW()+INTERVAL '1 hour', "
                    f"session_conflict_at={conflict or 'NULL'} WHERE id=$3",
                    owner, status, acc_id)
                cases[name] = acc_id

            for acc_id in cases.values():
                await conn.execute(_reset_sql(), acc_id, owner)

            rows = {r["id"]: r for r in await conn.fetch(
                "SELECT id, acc_status, status_reason, cooldown_until, risk_cleared_at "
                "FROM tg_accounts WHERE id = ANY($1::bigint[])", list(cases.values()))}

            for name, acc_id in cases.items():
                r = rows[acc_id]
                assert r["cooldown_until"] is None, f"{name}: кулдаун обязан сняться"
                assert r["risk_cleared_at"] is not None, f"{name}: отметка сброса риска"

            assert rows[cases["cooldown"]]["acc_status"] == "active", (
                "транзиентный 'cooldown' обязан сняться сразу, а не через час")
            assert rows[cases["cooldown"]]["status_reason"] is None, (
                "причина статуса обязана уйти вместе со статусом")
            assert rows[cases["banned"]]["acc_status"] == "banned", (
                "жёсткий статус трогать нельзя")
            assert rows[cases["conflict"]]["acc_status"] == "cooldown", (
                "при конфликте сессии статус не снимаем — сессию надо перезалить")
        finally:
            if cases:
                await conn.execute("DELETE FROM tg_accounts WHERE id = ANY($1::bigint[])",
                                   list(cases.values()))
    finally:
        await conn.close()
