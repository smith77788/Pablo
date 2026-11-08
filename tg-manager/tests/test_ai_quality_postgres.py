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


def test_прогноз_покупок_и_тег_сегмента_на_живой_базе():
    """SQL выборки покупателей (оплаченные и подтверждённые заказы, переписка
    ботов) и тег сегмента на карточках контактов — с проверкой, что тег
    снимается с выбывших и не дублируется."""
    from services import ai_predict as ap

    async def body():
        pool = await _pool()
        try:
            for t in ("bot_sales_orders", "bot_sales_dialogs", "unified_contacts"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", _OWNER)
            # 501 — постоянный, пропустил ритм: три покупки 100/90/80 дней назад.
            for days in (100, 90, 80):
                await pool.execute(
                    "INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, "
                    "customer_name, status, total_cents, created_at) "
                    "VALUES ($1, 77, 501, 'Оля', 'done', 1250, now() - make_interval(days => $2))",
                    _OWNER, days)
            await pool.execute(  # неподтверждённый заказ — не покупка
                "INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, status) "
                "VALUES ($1, 77, 502, 'new')", _OWNER)
            await pool.execute(
                "INSERT INTO bot_sales_dialogs (bot_id, customer_chat_id, owner_id, last_at, history) "
                "VALUES (77, 502, $1, now() - interval '2 hours', $2::jsonb)",
                _OWNER, '[{"role": "user", "content": "Сколько стоит доставка?"}]')
            for peer, tags in ((501, ["vip"]), (503, ["прогноз_под_угрозой"])):
                await pool.execute(
                    "INSERT INTO unified_contacts (owner_id, telegram_user_id, tags) "
                    "VALUES ($1, $2, $3)", _OWNER, peer, tags)
            f = await ap.forecast(pool, _OWNER)
            seg = {s["id"]: s["count"] for s in f["segments"]}
            assert seg["at_risk"] == 1 and seg["hot_leads"] == 1 and f["buyers"] == 1
            assert f["top"][0]["peer_user_id"] == 502
            assert not f["backtest"]["enough"]
            r = await ap.tag_segment(pool, _OWNER, "at_risk")
            assert (r["in_segment"], r["tagged"], r["added"], r["removed"]) == (1, 1, 1, 1)
            again = await ap.tag_segment(pool, _OWNER, "at_risk")
            assert (again["added"], again["removed"]) == (0, 0), "повтор не дублирует тег"
            tags = {row["telegram_user_id"]: row["tags"] for row in await pool.fetch(
                "SELECT telegram_user_id, tags FROM unified_contacts WHERE owner_id=$1", _OWNER)}
            assert tags[501] == ["vip", "прогноз_под_угрозой"] and tags[503] == []
            assert (await ap.tag_segment(pool, _OWNER, "нет"))["error"]
            assert (await ap.forecast(pool, _OTHER))["buyers"] == 0
        finally:
            for t in ("bot_sales_orders", "bot_sales_dialogs", "unified_contacts"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", _OWNER)
            await pool.close()
    _run(body())


def test_копайлот_помнит_реакции_и_зовёт_к_ждущим_на_живой_базе():
    from services import next_actions as na

    async def body():
        pool = await _pool()
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        await pool.execute(open(os.path.join(here, "schema_v281_copilot_feedback.sql"),
                                encoding="utf-8").read())
        try:
            await pool.execute("DELETE FROM copilot_feedback WHERE owner_id=$1", _OWNER)
            for t in ("bot_sales_orders", "bot_sales_dialogs"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", _OWNER)
            await pool.execute(
                "INSERT INTO bot_sales_dialogs (bot_id, customer_chat_id, owner_id, last_at, history) "
                "VALUES (77, 601, $1, now() - interval '2 hours', $2::jsonb)",
                _OWNER, '[{"role": "user", "content": "Беру, как оплатить?"}]')
            ids = [a["id"] for a in await na.compute_next_actions(pool, _OWNER, limit=8)]
            assert "contacts_hot_waiting" in ids and "contacts_hot_leads" in ids
            assert await na.record_feedback(pool, _OWNER, "contacts_hot_waiting", "snoozed")
            assert not await na.record_feedback(pool, _OWNER, "x", "неизвестно")
            await na.record_feedback(pool, _OWNER, "contacts_hot_leads", "opened")
            acts = {a["id"]: a for a in await na.compute_next_actions(pool, _OWNER, limit=8)}
            assert "contacts_hot_waiting" not in acts, "отложенная спрятана"
            assert acts["contacts_hot_leads"]["priority"] == 78
            stats = await na.feedback_stats(pool, _OTHER)
            assert stats == {}
        finally:
            await pool.execute("DELETE FROM copilot_feedback WHERE owner_id=$1", _OWNER)
            for t in ("bot_sales_orders", "bot_sales_dialogs"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", _OWNER)
            await pool.close()
    _run(body())


def test_прогноз_видит_отметки_воронки_и_сегмент_из_фразы_по_прогнозу(monkeypatch):
    """«Купил» и «Готов купить» на лестнице воронки контакта (virtual_layer) —
    у людей без заказов бота; сегмент из фразы «спрашивали цену и не купили»
    ложится тегом прогноза и считается тем же count_segment."""
    from services import ai_gateway as gw, ai_predict as ap, ai_segment as seg

    async def _chat(messages, **k):
        return gw.AiReply(text='{"forecast": "hot_leads", "unsupported": "за неделю"}',
                          providers=["p"])
    monkeypatch.setattr(gw, "chat", _chat)

    async def body():
        pool = await _pool()
        try:
            for t in ("bot_sales_orders", "bot_sales_dialogs", "unified_contacts",
                      "virtual_states", "virtual_state_history"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", _OWNER)
            buyer = await pool.fetchval(
                "INSERT INTO unified_contacts (owner_id, telegram_user_id, display_name) "
                "VALUES ($1, 701, 'Ира') RETURNING id::text", _OWNER)
            lead = await pool.fetchval(
                "INSERT INTO unified_contacts (owner_id, telegram_user_id) "
                "VALUES ($1, 702) RETURNING id::text", _OWNER)
            await pool.execute(
                "INSERT INTO virtual_state_history (owner_id, entity_type, entity_id, "
                "state_key, from_value, to_value, created_at) "
                "VALUES ($1, 'user', $2, 'funnel', 'ready', 'purchased', now() - interval '3 days')",
                _OWNER, buyer)
            await pool.execute(
                "INSERT INTO virtual_states (owner_id, entity_type, entity_id, state_key, value) "
                "VALUES ($1, 'user', $2, 'funnel', 'ready')", _OWNER, lead)
            f = await ap.forecast(pool, _OWNER)
            counts = {s["id"]: s["count"] for s in f["segments"]}
            assert counts["new"] == 1 and counts["hot_leads"] == 1 and f["buyers"] == 1
            out = await seg.from_text(pool, _OWNER, "спрашивали цену и не купили за неделю")
            assert out["filters"] == {"tag": "прогноз_горячие"} and out["total"] == 1
            assert "прогноз: Горячие без покупки" in out["explained"]
            assert "неделю" in out["unsupported"]
            assert await ap.refresh_tags(pool, _OWNER) == ["hot_leads"]
        finally:
            for t in ("bot_sales_orders", "bot_sales_dialogs", "unified_contacts",
                      "virtual_states", "virtual_state_history"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", _OWNER)
            await pool.close()
    _run(body())
