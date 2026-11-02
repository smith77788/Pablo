"""Когорты, LTV и валюта владельца — против настоящей схемы.

Модули читают источники «мягко»: таблицы, которой нет, они не замечают, и
отчёт не падает. Ровно поэтому ошибка в тексте запроса прошла бы молча —
экран показал бы пустоту. Пул здесь записывает ЛЮБУЮ ошибку запроса, и тест
требует, чтобы ошибок не было, а числа сходились с засеянными данными.

Нужен живой Postgres (INFRAGRAM_TEST_DSN), иначе тест пропускается.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993711
BOT_ID = 880099711
PEER_A = 7_000_711          # подписался на бота, купил в долларах
PEER_B = 7_000_712          # получил рассылку, купил в рублях, потом написал
PEER_C = 7_000_713          # купил, не оставив касаний

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _Strict:
    def __init__(self, pool):
        self._p = pool
        self.errors = []

    async def _wrap(self, name, q, *a):
        try:
            return await getattr(self._p, name)(q, *a)
        except Exception as exc:
            self.errors.append(f"{exc!r} :: {q[:120]}")
            raise

    async def fetch(self, q, *a):
        return await self._wrap("fetch", q, *a)

    async def fetchrow(self, q, *a):
        return await self._wrap("fetchrow", q, *a)

    async def fetchval(self, q, *a):
        return await self._wrap("fetchval", q, *a)

    async def execute(self, q, *a):
        return await self._wrap("execute", q, *a)


async def _cleanup(p):
    await p.execute("DELETE FROM owner_money_settings WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM inbox_messages WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


async def _no_network():
    raise AssertionError("в тесте курс не запрашивается из сети")


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
    now = datetime.now(timezone.utc)

    async def _seed():
        # Свежий курс: отчёт не пойдёт за ним в сеть.
        await p.execute(
            """INSERT INTO fx_rates(currency, rub_per_unit, fetched_at) VALUES('USD', 90, now())
               ON CONFLICT (currency) DO UPDATE SET rub_per_unit=90, fetched_at=now()""")
        await p.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'cohort_bot',TRUE,$3)", f"1234:co-{BOT_ID}", BOT_ID, OWNER)
        await p.execute(
            "INSERT INTO bot_users(bot_id, user_id, first_name, first_seen) VALUES($1,$2,'Аня',$3)",
            BOT_ID, PEER_A, now - timedelta(hours=3))
        cid = await p.fetchval(
            "INSERT INTO dm_campaigns(owner_id, name, text_template, status) "
            "VALUES($1,'Осенняя','Привет','done') RETURNING id", OWNER)
        await p.execute(
            "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, sent_at) "
            "VALUES($1,1,$2,'sent',$3)", cid, PEER_B, now - timedelta(hours=4))
        await p.execute(
            "INSERT INTO bot_sales_orders(owner_id, bot_id, customer_chat_id, total_cents, currency, status, created_at) "
            "VALUES($1,$2,$3,1000,'USD','done',$6), ($1,$2,$4,45000,'RUB','confirmed',$6),"
            "      ($1,$2,$5,10000,'RUB','done',$6), ($1,$2,$3,99900,'RUB','cancelled',$6)",
            OWNER, BOT_ID, PEER_A, PEER_B, PEER_C, now - timedelta(hours=2))
        await p.execute(
            "INSERT INTO inbox_messages(owner_id, account_id, peer_user_id, msg_id, text_enc, msg_date) "
            "VALUES($1,1,$2,1,'ENC:x',$3)", OWNER, PEER_B, now - timedelta(hours=1))

    _run(_seed())
    yield p
    _run(_cleanup(p))
    _run(p.close())


def test_cohort_report_on_real_schema(pool):
    from services import cohorts

    sp = _Strict(pool)
    out = _run(cohorts.report(sp, OWNER, grain="month", fetch_rates=_no_network))
    assert sp.errors == []
    t = out["totals"]
    assert t["people"] == 3 and t["buyers"] == 3 and t["orders"] == 3
    # $10 по 90 ₽ + 450 ₽ + 100 ₽; отменённый заказ — не выручка.
    assert t["revenue"] == 1450.0
    src = {s["label"]: s for s in out["sources"]}
    assert src["бот @cohort_bot"]["revenue"] == 900.0
    assert src["рассылка «Осенняя»"]["revenue"] == 450.0
    assert src[cohorts.UNKNOWN_SOURCE]["revenue"] == 100.0
    assert out["cohorts"][0]["active"][0] == 1.0
    assert out["money"]["currency"] == "RUB" and out["unconverted"] == []


def test_owner_currency_round_trip(pool):
    from services import cohorts, owner_currency

    sp = _Strict(pool)
    _run(owner_currency.set_settings(sp, OWNER, "USD", {"RUB": "0,01"}))
    st = _run(owner_currency.get_settings(sp, OWNER))
    assert st == {"currency": "USD", "manual_rates": {"RUB": 0.01}}
    out = _run(cohorts.report(sp, OWNER, grain="week", fetch_rates=_no_network))
    assert sp.errors == []
    # $10 + 550 ₽ по своему курсу 0,01 $ за рубль.
    assert out["totals"]["revenue"] == 15.5
    assert out["money"]["currency"] == "USD"
