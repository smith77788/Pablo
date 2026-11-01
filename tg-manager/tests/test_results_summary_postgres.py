"""«Итоги» считают результат владельца по настоящим таблицам.

Экран «Итоги» и сводка в боте показывают четыре числа: деньги, лиды, ответы и
охват за период. Проверка текста SQL не говорит, примет ли Postgres запрос
(см. CLAUDE.md: «$1::bigint[] против uuid» падал всегда), поэтому здесь
расчёт прогоняется против живой базы:

* считается только своё: бот, заказ и сделка другого владельца в итог не
  попадают;
* текущий период отделён от прошлого, и изменение в процентах честное;
* заказы в статусе «новый» и «отменён» деньгами не считаются;
* упавшая выборка даёт «нет данных», а не ноль и не пустой экран.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990771
OTHER = 990772
BOT = 7700990771
BOT_OTHER = 7700990772

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        # Пул создаётся ВНУТРИ цикла: иначе он привязывается к другому.
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")
    _run(_seed(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


async def _cleanup(p):
    for bot in (BOT, BOT_OTHER):
        await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", bot)
    await p.execute("DELETE FROM bot_sales_orders WHERE owner_id = ANY($1::bigint[])",
                    [OWNER, OTHER])
    await p.execute("DELETE FROM crm_deals WHERE owner_id = ANY($1::bigint[])",
                    [OWNER, OTHER])


async def _seed(p):
    await _cleanup(p)
    for bot, owner in ((BOT, OWNER), (BOT_OTHER, OTHER)):
        await p.execute(
            "INSERT INTO managed_bots (token, bot_id, username, added_by) "
            "VALUES ($1, $2, $3, $4)",
            f"tok-{bot}", bot, f"b{bot}", owner)
    # Лиды: 3 новых за неделю и 1 в прошлой неделе у владельца, 5 у чужого.
    for i, ago in enumerate((1, 2, 3, 10)):
        await p.execute(
            "INSERT INTO bot_users (bot_id, user_id, first_seen) "
            "VALUES ($1, $2, now() - make_interval(days => $3))", BOT, 1000 + i, ago)
    for i in range(5):
        await p.execute(
            "INSERT INTO bot_users (bot_id, user_id, first_seen) VALUES ($1, $2, now())",
            BOT_OTHER, 2000 + i)
    # Ответы: 2 входящих и 1 исходящее за неделю.
    sid = await p.fetchval(
        "INSERT INTO relay_sessions (bot_id, user_id) VALUES ($1, 1000) RETURNING id", BOT)
    for direction in ("in", "in", "out"):
        await p.execute(
            "INSERT INTO relay_messages (session_id, direction, text) VALUES ($1, $2, 'x')",
            sid, direction)
    # Охват: рассылка на 40 доставленных за неделю и 10 в прошлой.
    for sent, ago in ((40, 1), (10, 9)):
        await p.execute(
            "INSERT INTO broadcasts (bot_id, message_text, sent_count, created_by, created_at) "
            "VALUES ($1, 'x', $2, $3, now() - make_interval(days => $4))",
            BOT, sent, OWNER, ago)
    # Деньги: подтверждённый и выполненный заказы считаются, новый и отменённый — нет.
    for cents, status in ((1500, "confirmed"), (2500, "done"), (9900, "new"),
                          (9900, "cancelled")):
        await p.execute(
            "INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, total_cents, "
            "currency, status) VALUES ($1, $2, 1, $3, 'USD', $4)",
            OWNER, BOT, cents, status)
    await p.execute(
        "INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, total_cents, "
        "currency, status) VALUES ($1, $2, 1, 777700, 'USD', 'done')", OTHER, BOT_OTHER)
    await p.execute(
        "INSERT INTO crm_deals (owner_id, title, stage, value) VALUES ($1, 'd', 'won', 300)",
        OWNER)


def test_counts_only_own_and_current_period(pool):
    from services import results_summary as rs

    s = _run(rs.compute(pool, OWNER, 7, with_attention=False))
    assert s["leads"]["value"] == 3 and s["leads"]["prev"] == 1
    assert s["leads"]["delta_pct"] == 200
    assert s["replies"]["value"] == 2
    assert s["reach"]["value"] == 40 and s["reach"]["prev"] == 10
    m = s["money"]
    assert m["by_currency"] == {"USD": 4000}, m
    assert m["orders"] == 2
    assert m["deals_won"] == 1 and m["deals_value"] == 300.0


def test_other_owner_sees_only_theirs(pool):
    from services import results_summary as rs

    s = _run(rs.compute(pool, OTHER, 7, with_attention=False))
    assert s["leads"]["value"] == 5
    assert s["replies"]["value"] == 0
    assert s["money"]["by_currency"] == {"USD": 777700}


def test_attention_is_capped_at_three(pool):
    from services import results_summary as rs

    s = _run(rs.compute(pool, OWNER, 7))
    assert len(s["attention"]) <= rs.ATTENTION_LIMIT
    assert s["attention_total"] >= len(s["attention"])


def test_failed_query_is_no_data_not_zero():
    """Сбой выборки — «нет данных», а не ноль: ноль соврал бы, что продаж нет."""
    from services import results_summary as rs

    class Broken:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("база недоступна")

        async def fetch(self, *a, **k):
            raise RuntimeError("база недоступна")

    s = _run(rs.compute(Broken(), OWNER, 7, with_attention=False))
    assert s["leads"]["value"] is None
    assert s["money"] is None
    assert "нет данных" in rs.bot_text(s)
