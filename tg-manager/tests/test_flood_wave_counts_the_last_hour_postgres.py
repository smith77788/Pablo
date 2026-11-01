"""Детектор волны флудов на настоящей схеме: считает последний час, а не неделю.

Пара к `test_an_anomaly_alert_is_sent_once_and_can_be_muted.py`: там поведение
проверено на поддельном пуле, и проверка по тексту SQL не доказывает, что база
этот запрос примет (подзапросы, JOIN журнала флудов с аккаунтами). Здесь тот
же `_detect_flood_wave` идёт против схемы, которую накатывает conftest.

Сценарий из жалобы: неделю назад флудили почти все аккаунты (`flood_count_7d`
у всех > 0 и старые строки журнала), а за последний час — ни одного флуда.
Тревоги быть не должно. Затем — свежая волна за час: тревога должна быть.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993512
N = 10

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p) -> None:
    await p.execute(
        "DELETE FROM account_flood_log WHERE account_id IN "
        "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


def _seed(pool, recent: int) -> None:
    async def _go():
        await _cleanup(pool)
        ids = []
        for i in range(N):
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, flood_count_7d) "
                "VALUES($1, $2, 'test-session', TRUE, 3) RETURNING id",
                OWNER, f"+7099351{i:04d}"))
        for aid in ids:     # неделю назад флудили все
            await pool.execute(
                "INSERT INTO account_flood_log(account_id, flood_seconds, created_at) "
                "VALUES($1, 60, NOW() - INTERVAL '5 days')", aid)
        for aid in ids[:recent]:   # а за последний час — только эти
            for _ in range(2):
                await pool.execute(
                    "INSERT INTO account_flood_log(account_id, flood_seconds) "
                    "VALUES($1, 60)", aid)
    _run(_go())


def test_last_weeks_wave_is_quiet_now(pool):
    from services import anomaly_detector as AD
    _seed(pool, recent=0)
    assert _run(AD._detect_flood_wave(pool, OWNER)) == []


def test_a_wave_in_the_last_hour_is_seen(pool):
    from services import anomaly_detector as AD
    _seed(pool, recent=9)
    got = _run(AD._detect_flood_wave(pool, OWNER))
    assert len(got) == 1 and got[0].severity == "critical", got
    assert got[0].affected_count == 9
