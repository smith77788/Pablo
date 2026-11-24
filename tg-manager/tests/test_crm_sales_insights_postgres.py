"""Аналитика продаж (services/crm_sales_insights) на ЖИВОЙ базе: источник по
аккаунту флота, последнее касание из журнала и напоминание читаются настоящими
запросами.

Почему живой Postgres: источник держится только join'ом на tg_accounts
(у contact_sources нет owner_id, schema_v138), а касание — LATERAL-подзапросами
по двум журналам. Чужой аккаунт в источниках — утечка, и на заглушке её не
видно.

Запуск: INFRAGRAM_TEST_DSN, как в tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991872
OTHER = 991873
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _wipe(p)
    _run(p.close())


def _wipe(p):
    for o in (OWNER, OTHER):
        _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", o))
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM crm_deals WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM crm_stage_history WHERE owner_id=$1", OWNER))


@pytest.fixture(autouse=True)
def _clean(pool):
    _wipe(pool)
    yield


def _contact(pool, name):
    return _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1,$2) RETURNING id",
        OWNER, name))


def _account(pool, owner, phone, first_name=""):
    return _run(pool.fetchval(
        "INSERT INTO tg_accounts (owner_id, phone, session_str, first_name) "
        "VALUES ($1,$2,'x',$3) RETURNING id", owner, phone, first_name))


def _source(pool, cid, acc, days_ago):
    _run(pool.execute(
        "INSERT INTO contact_sources (contact_id, account_id, first_seen_at) "
        "VALUES ($1,$2, now() - make_interval(days => $3))", cid, acc, days_ago))


def test_report_runs_against_the_real_schema(pool):
    from services import crm_sales_insights as si
    from services.contacts_hub.crm_engine import log_crm_activity, upsert_crm

    mine_first = _account(pool, OWNER, "+70000000001", "Первый")
    mine_late = _account(pool, OWNER, "+70000000002", "Второй")
    foreign = _account(pool, OTHER, "+70000000003", "Чужой")

    a, b, c = _contact(pool, "Анна"), _contact(pool, "Борис"), _contact(pool, "Вера")
    _source(pool, a, mine_late, 1)
    _source(pool, a, mine_first, 5)          # нашёл раньше — он и источник
    _source(pool, b, foreign, 9)             # чужой аккаунт источником не считается
    _run(upsert_crm(pool, OWNER, str(a), {"stage": "lead", "deal_value": 1000, "currency": "RUB"}))
    _run(upsert_crm(pool, OWNER, str(a), {"stage": "won"}))
    _run(upsert_crm(pool, OWNER, str(b), {"stage": "proposal", "deal_value": 50, "currency": "USD"}))
    _run(upsert_crm(pool, OWNER, str(c), {"stage": "contact"}))
    # c — давно без касания: сдвигаем всю историю и создание в прошлое
    _run(pool.execute(
        "UPDATE crm_stage_history SET changed_at = now() - interval '40 days' "
        "WHERE owner_id=$1 AND deal_key=$2", OWNER, str(c)))
    _run(pool.execute(
        "UPDATE crm_stage_history SET changed_at = now() - interval '40 days' "
        "WHERE owner_id=$1 AND deal_key=$2", OWNER, str(b)))
    # у b свежее касание в журнале — не брошена
    _run(log_crm_activity(pool, OWNER, str(b), "call", "созвон"))
    did = _run(pool.fetchval(
        "INSERT INTO crm_deals (owner_id, title, stage, value) VALUES ($1,'Окна','lead',100) RETURNING id",
        OWNER))
    _run(pool.execute("UPDATE crm_deals SET stage='lost' WHERE id=$1", did))

    rep = _run(si.report(pool, OWNER, 90))
    src = {s["source"]: s for s in rep["sources"]}
    assert str(mine_first) in src and src[str(mine_first)]["label"] == "Первый"
    assert src[str(mine_first)]["won"] == 1
    assert str(mine_late) not in src and str(foreign) not in src, rep["sources"]
    assert src[si.NO_SOURCE]["leads"] == 3   # b (чужой источник), c и простая сделка
    assert [x["deal_key"] for x in rep["neglected"]] == [str(c)], rep["neglected"]
    assert rep["losses"][0]["stage"] == "lead" and rep["losses"][0]["label"] == "Лид"
    assert rep["period"]["won"] == 1 and rep["period"]["lost"] == 1
    assert rep["period"]["won_by_currency"] == {"RUB": 1000.0}
