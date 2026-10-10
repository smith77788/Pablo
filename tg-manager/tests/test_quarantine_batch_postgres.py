"""Карантин всего флота — один запрос, и он отвечает то же, что поштучный.

ЧТО БЫЛО. Гейт карантина спрашивали про аккаунт: `is_account_quarantined(pool,
id)`. Но спрашивают его про СПИСОК — перед каждой массовой операцией (инвайт,
рассылка, публикация, страйк) и на каждую отрисовку экрана флота. На флоте в 200
аккаунтов это 200 round-trip подряд ПЕРЕД первым действием, а в админ-слое
(`va_control.eligible_accounts`) те же 200 уходили через `asyncio.gather`, то
есть одновременно, в пул из 20 соединений.

И главное следствие — не ожидание. Проверка принципиально fail-open: любая
ошибка читается как «аккаунт чист». Пул с command_timeout 30 с, выеденный
собственными же проверками, начинает отдавать таймауты — и гейт отвечает «все
чисты» ровно на том флоте, где он нужнее. Предохранитель отключал себя под
нагрузкой, молча.

ЧТО ТЕПЕРЬ. `quarantined_accounts(pool, ids)` — один запрос с `= ANY($1)`;
одиночная проверка стала его частным случаем, поэтому условие карантина живёт в
одном месте и разъехаться не может.

Живой Postgres: заглушка пула не проверяет ни JOIN, ни связывание массива
(`int[]` против `bigint[]` — ровно тот класс ошибок, который на юнит-тестах
невидим). Без INFRAGRAM_TEST_DSN — скип. Инструкция по стенду —
в tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 771203


async def _mk_account(conn, n: int) -> int:
    return await conn.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
        "VALUES($1, $2, 'sess', TRUE) RETURNING id",
        OWNER, f"+7995{n:07d}")


@pytest.mark.asyncio
async def test_batch_gate_agrees_with_the_single_one():
    from services.infra_memory import is_account_quarantined, quarantined_accounts

    conn = await asyncpg.connect(_DSN)
    ids: list[int] = []
    try:
        for n in range(5):
            ids.append(await _mk_account(conn, n))
        clean, banned, blocked, cleared, warned = ids

        # Критическое ограничение и ограничение по типу события — оба считаются.
        await conn.execute(
            "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
            "VALUES($1, $2, 'ban_detected', 'critical')", OWNER, banned)
        await conn.execute(
            "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
            "VALUES($1, $2, 'channel_blocked', 'warning')", OWNER, blocked)
        # Владелец снял риск ПОСЛЕ события — аккаунт обязан быть чистым.
        await conn.execute(
            "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
            "VALUES($1, $2, 'ban_detected', 'critical')", OWNER, cleared)
        await conn.execute(
            "UPDATE tg_accounts SET risk_cleared_at=NOW() WHERE id=$1", cleared)
        # Нестрашное событие (не critical и не про ban/block/restrict) — не карантин.
        await conn.execute(
            "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
            "VALUES($1, $2, 'slow_mode_detected', 'warning')", OWNER, warned)

        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
        try:
            got = await quarantined_accounts(pool, ids)
            assert got == {banned, blocked}, (
                f"пакетная проверка ответила {got}, а под карантином "
                f"{ {banned, blocked} }")

            # Та же правда по одному — иначе две двери разъехались.
            for acc_id in ids:
                single = await is_account_quarantined(pool, acc_id)
                assert single is (acc_id in got), (
                    f"аккаунт {acc_id}: поштучно {single}, в пакете "
                    f"{acc_id in got}")

            # Чужих в ответе нет, и несуществующий id ничего не ломает.
            assert await quarantined_accounts(pool, [clean]) == set()
            assert await quarantined_accounts(pool, [-1, 0, clean]) == set()
        finally:
            await pool.close()
    finally:
        await conn.execute("DELETE FROM restriction_events WHERE account_id = ANY($1::bigint[])", ids)
        await conn.execute("DELETE FROM tg_accounts WHERE id = ANY($1::bigint[])", ids)
        await conn.close()


@pytest.mark.asyncio
async def test_whole_fleet_costs_one_query():
    """Двести аккаунтов — один запрос. Это и есть предмет правки."""
    from services import infra_memory

    conn = await asyncpg.connect(_DSN)
    ids: list[int] = []
    try:
        for n in range(200):
            ids.append(await _mk_account(conn, 1000 + n))
        # Каждый десятый под баном — ответ обязан быть точным, а не «всё чисто».
        banned = ids[::10]
        await conn.executemany(
            "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
            "VALUES($1, $2, 'ban_detected', 'critical')",
            [(OWNER, acc_id) for acc_id in banned])

        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)

        class _Counting:
            """Живой пул со счётчиком запросов (у asyncpg.Pool методы read-only)."""

            def __init__(self, inner):
                self._inner = inner
                self.asked: list[str] = []

            async def fetch(self, query, *args, **kw):
                self.asked.append(query)
                return await self._inner.fetch(query, *args, **kw)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        counting = _Counting(pool)
        try:
            got = await infra_memory.quarantined_accounts(counting, ids)
            assert got == set(banned), f"найдено {len(got)} из {len(banned)}"
            assert len(counting.asked) == 1, (
                f"на 200 аккаунтов ушло {len(counting.asked)} запросов вместо одного")
        finally:
            await pool.close()
    finally:
        await conn.execute("DELETE FROM restriction_events WHERE account_id = ANY($1::bigint[])", ids)
        await conn.execute("DELETE FROM tg_accounts WHERE id = ANY($1::bigint[])", ids)
        await conn.close()
