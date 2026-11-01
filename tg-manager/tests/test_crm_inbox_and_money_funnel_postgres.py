"""Инбокс, путь клиента и воронка денег — против настоящей схемы.

Все три модуля читают источники «мягко»: таблицы, которой нет, они не
замечают, и карточка не падает. Ровно поэтому ошибка в тексте запроса
(не та колонка, не тот тип) прошла бы молча — экран показал бы пустоту.
Здесь пул обёрнут так, что ЛЮБАЯ ошибка запроса записывается, и тест требует,
чтобы ошибок не было, а числа сходились с засеянными данными.

Нужен живой Postgres (INFRAGRAM_TEST_DSN), иначе тест пропускается.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993611
BOT_ID = 880099611
PEER_A = 7_000_611          # получил рассылку, ответил на аккаунт, купил у бота
PEER_B = 7_000_612          # написал в релей бота, ему никто не писал
ACC = 99611

_LOOP = None
_SEEDED: dict = {}


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _Strict:
    """Пул, который запоминает каждую ошибку запроса, прежде чем отдать её."""

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
    await p.execute("DELETE FROM inbox_messages WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM campaign_spend WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_sales_dialogs WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM vault_messages WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM relay_sessions WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


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
        await p.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'crm_funnel_bot',TRUE,$3)", f"1234:crm-{BOT_ID}", BOT_ID, OWNER)
        cid = await p.fetchval(
            "INSERT INTO dm_campaigns(owner_id, name, text_template, status) "
            "VALUES($1,'Осенняя','Привет','done') RETURNING id", OWNER)
        await p.execute(
            "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, sent_at) "
            "VALUES($1,$2,$3,'sent',$4), ($1,$2,$5,'failed',$4)",
            cid, ACC, PEER_A, now - timedelta(days=3), 7_000_699)
        await p.execute(
            "INSERT INTO bot_users(bot_id, user_id, first_name, first_seen) VALUES($1,$2,'Аня',$3)",
            BOT_ID, PEER_A, now - timedelta(days=2, hours=12))
        await p.execute(
            "INSERT INTO unified_contacts(owner_id, telegram_user_id, first_name) VALUES($1,$2,'Аня')",
            OWNER, PEER_A)
        await p.execute(
            "INSERT INTO bot_sales_orders(owner_id, bot_id, customer_chat_id, total_cents, currency, status, created_at) "
            "VALUES($1,$2,$3,250000,'RUB','done',$4), ($1,$2,$3,99900,'RUB','cancelled',$4)",
            OWNER, BOT_ID, PEER_A, now - timedelta(days=1))
        await p.execute(
            "INSERT INTO bot_sales_dialogs(bot_id, customer_chat_id, owner_id, history, msg_count, last_at) "
            "VALUES($1,$2,$3,$4::jsonb,2,$5)",
            BOT_ID, PEER_A, OWNER,
            json.dumps([{"role": "user", "content": "беру"},
                        {"role": "assistant", "content": "оформляю"}]),
            now - timedelta(days=1, hours=1))
        sid = await p.fetchval(
            "INSERT INTO relay_sessions(bot_id, user_id, first_name, username) "
            "VALUES($1,$2,'Борис','boris') RETURNING id", BOT_ID, PEER_B)
        await p.execute(
            "INSERT INTO relay_messages(session_id, direction, text, created_at) "
            "VALUES($1,'in','есть доставка?',$2)", sid, now - timedelta(hours=2))
        return cid

    cid = _run(_seed())
    _SEEDED['campaign_id'] = cid
    yield p
    _run(_cleanup(p))
    _run(p.close())


def test_inbox_reads_all_sources_without_query_errors(pool, monkeypatch):
    monkeypatch.setattr("services.token_vault.encrypt_token", lambda t: "ENC:" + t)
    monkeypatch.setattr("services.token_vault.decrypt_token", lambda t: t[4:])
    from services import unified_inbox as ui

    sp = _Strict(pool)
    peer = {"peer_user_id": PEER_A, "peer_name": "Аня", "peer_username": "anya"}
    now = datetime.now(timezone.utc)
    assert _run(ui.record_incoming(sp, OWNER, ACC, peer, 501, "а сколько стоит?",
                                   now - timedelta(days=2))) is True
    assert _run(ui.record_incoming(sp, OWNER, ACC, peer, 501, "а сколько стоит?",
                                   now - timedelta(days=2))) is False

    res = _run(ui.list_threads(sp, OWNER))
    assert sp.errors == []
    by = {t["peer_user_id"]: t for t in res["threads"]}
    assert set(by) == {PEER_A, PEER_B}
    assert {c["source"] for c in by[PEER_A]["channels"]} == {"fleet", "bot"}
    assert by[PEER_A]["contact_id"]
    assert by[PEER_B]["waiting"] is True and by[PEER_B]["name"] == "Борис"
    assert res["waiting"] >= 1 and res["unread"] == 1

    th = _run(ui.get_thread(sp, OWNER, PEER_A))
    assert sp.errors == []
    assert [m["text"] for m in th["messages"]][:1] == ["а сколько стоит?"]
    assert _run(ui.mark_read(sp, OWNER, PEER_A)) == 1
    assert sp.errors == []

    waiting = _run(ui.list_threads(sp, OWNER, only_waiting=True))
    assert all(t["waiting"] for t in waiting["threads"])


def test_journey_parts_run_on_real_schema(pool, monkeypatch):
    monkeypatch.setattr("services.token_vault.decrypt_token", lambda t: t[4:])
    from services.contacts_hub.journey import journey_parts
    from services.contacts_hub.timeline import build_journey

    sp = _Strict(pool)
    parts = _run(journey_parts(sp, OWNER, PEER_A))
    assert sp.errors == []
    assert [r["campaign"] for r in parts["received"]] == ["Осенняя"]
    assert len(parts["orders"]) == 2
    assert parts["bot_sources"] and parts["bot_sources"][0]["label"] == "бот @crm_funnel_bot"
    j = build_journey(sources=parts["bot_sources"], received=parts["received"],
                      replies=parts["replies"], orders=parts["orders"])
    assert j["bought"]["orders"] == 1
    assert j["bought"]["revenue"][0]["total_cents"] == 250000


def test_money_funnel_builds_on_real_schema(pool):
    from services import money_funnel as mf

    sp = _Strict(pool)
    cid = _SEEDED['campaign_id']
    _run(mf.set_spend(sp, OWNER, cid, 1000, "RUB"))
    out = _run(mf.build(sp, OWNER, 30))
    assert sp.errors == []
    row = [r for r in out["campaigns"] if r["campaign_id"] == cid][0]
    assert row["name"] == "Осенняя"
    # Недоставленное (failed) — не охват.
    assert row["steps"]["reached"] == 1
    assert row["steps"]["subscribed"] == 1
    assert row["steps"]["bought"] == 1
    assert row["revenue"] == [{"currency": "RUB", "total_cents": 250000}]
    assert row["spend"] == {"amount": 1000.0, "currency": "RUB"}
    # Человек из релея, которому кампании не писали, — «Без рассылки».
    organic = [r for r in out["campaigns"] if r["campaign_id"] == mf.NO_CAMPAIGN][0]
    assert organic["steps"]["replied"] >= 1
