"""Журнал ответов ИИ и учёт расхода — на НАСТОЯЩЕМ Postgres.

Заглушка пула не проверяет ни типы параметров, ни сам SQL: percentile_cont,
make_interval и ON CONFLICT по составному ключу видно только в базе.
Запуск — как у остальных postgres-тестов (нужен INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

_OWNER, _OTHER = 990_262_001, 990_262_002


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _pool():
    import asyncpg
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conn = await asyncpg.connect(DSN)
    await conn.execute(open(os.path.join(here, "schema_v266_ai_gateway.sql"),
                            encoding="utf-8").read())
    for t in ("ai_reply_log", "llm_owner_usage"):
        await conn.execute(f"DELETE FROM {t} WHERE owner_id = ANY($1::bigint[])",
                           [_OWNER, _OTHER])
    await conn.close()
    return await asyncpg.create_pool(DSN, min_size=1, max_size=2)


def test_журнал_оценка_сводка_и_чужая_запись():
    from services import ai_quality as aq

    async def body():
        pool = await _pool()
        try:
            a = await aq.log_reply(owner_id=_OWNER, source="sales", source_id=5,
                                   user_text="сколько?", reply="12.50 USD",
                                   provider="openai", model="m", latency_ms=800, pool=pool)
            b = await aq.log_reply(owner_id=_OWNER, source="sales", source_id=5,
                                   user_text="скидку?", reply="Уточню", guard="discount",
                                   guard_detail="40%", latency_ms=1200, pool=pool)
            await aq.log_reply(owner_id=_OWNER, source="sales", user_text="x", reply="y",
                               guard="неизвестно", pool=pool)
            assert a and b
            assert await aq.rate_reply(pool, _OWNER, a, 1)
            assert await aq.rate_reply(pool, _OWNER, b, -1)
            assert not await aq.rate_reply(pool, _OTHER, a, -1), "чужую запись оценить нельзя"
            page = await aq.list_replies(pool, _OWNER, limit=2)
            assert page["total"] == 3 and len(page["items"]) == 2 and page["remaining"] == 1
            bad = await aq.list_replies(pool, _OWNER, only="bad")
            assert [r["id"] for r in bad["items"]] == [b]
            guarded = await aq.list_replies(pool, _OWNER, only="guard")
            assert [r["guard"] for r in guarded["items"]] == ["discount"]
            s = await aq.summary(pool, _OWNER, days=7)
            assert (s["replies"], s["good"], s["bad"], s["guard_discount"]) == (3, 1, 1, 1)
            assert s["bad_share"] == 0.5 and s["latency_p90_ms"] >= 800
            assert (await aq.list_replies(pool, _OTHER))["total"] == 0
        finally:
            await pool.close()
    _run(body())


def test_учёт_расхода_складывается_за_день():
    from services import ai_gateway as gw

    async def body():
        pool = await _pool()
        gw.attach(pool)
        try:
            for ok in (True, False):
                r = gw.AiReply(text="ок" if ok else None, input_tokens=10, output_tokens=4)
                await gw._record(_OWNER, gw.TASK_CUSTOMER, r)
            rows = await gw.owner_usage(pool, _OWNER, days=1)
            assert len(rows) == 1
            row = rows[0]
            assert (row["calls"], row["ok"], row["input_tokens"], row["output_tokens"]) == (2, 1, 20, 8)
        finally:
            gw.attach(None)
            await pool.close()
    _run(body())


def test_воронка_продаж_считает_заказы_без_оператора():
    from services import ai_quality as aq

    async def body():
        pool = await _pool()
        try:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", _OWNER)
            await pool.execute("DELETE FROM bot_sales_dialogs WHERE owner_id=$1", _OWNER)
            for chat, off in ((1, False), (2, True), (3, False), (4, False)):
                await pool.execute(
                    "INSERT INTO bot_sales_dialogs (bot_id, customer_chat_id, owner_id, handed_off) "
                    "VALUES (77, $1, $2, $3)", chat, _OWNER, off)
            for chat, status, paid in ((1, "confirmed", False), (2, "confirmed", False),
                                       (3, "new", False), (4, "done", True)):
                await pool.execute(
                    "INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, status, paid_at) "
                    "VALUES ($1, 77, $2, $3, CASE WHEN $4 THEN now() END)",
                    _OWNER, chat, status, paid)
            f = await aq.sales_funnel(pool, _OWNER, days=7)
            assert (f["dialogs"], f["handed_off"], f["orders"], f["confirmed"], f["paid"],
                    f["confirmed_without_operator"]) == (4, 1, 4, 3, 1, 2)
            assert f["conversion"] == 0.75 and f["resolved_without_operator"] == 0.75
            assert (await aq.sales_funnel(pool, _OTHER))["conversion"] is None
        finally:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", _OWNER)
            await pool.execute("DELETE FROM bot_sales_dialogs WHERE owner_id=$1", _OWNER)
            await pool.close()
    _run(body())


def test_сегмент_из_фразы_считает_размер_на_живой_базе(monkeypatch):
    """SQL словаря владельца (теги через unnest, группы) и подсчёт среза тем же
    count_segment, что у превью рассылки, — на настоящей схеме."""
    from services import ai_gateway as gw, ai_segment as seg

    async def _chat(messages, **k):
        return gw.AiReply(text='{"gender": "f", "unsupported": "купили на прошлой неделе"}',
                          providers=["p"])
    monkeypatch.setattr(gw, "chat", _chat)

    async def body():
        pool = await _pool()
        try:
            out = await seg.from_text(pool, _OWNER, "женщины, купившие на прошлой неделе")
            assert out["filters"] == {"gender": "f"} and out["total"] == 0
            assert out["explained"] == ["женщины"] and "прошлой неделе" in out["unsupported"]
        finally:
            await pool.close()
    _run(body())
