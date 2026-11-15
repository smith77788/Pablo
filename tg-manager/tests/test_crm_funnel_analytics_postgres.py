"""История стадий сделок пишется на ЖИВОЙ базе — каждой дверью — и отчёт по ней
собирается настоящими запросами.

Почему живой Postgres: история пишется триггером (schema_v363), и вопрос ровно
в том, сработает ли он на настоящей таблице при настоящей записи — экрана
контакта (crm_engine.upsert_crm), простой сделки, слияния дублей. На заглушке
триггера нет вовсе.

ОБРАТНЫЙ КОНТРОЛЬ: без schema_v363 (DROP TRIGGER) краснеют все проверки
истории; без переноса ключа истории в триггере при смене contact_id —
проверка слияния.

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
OWNER = 991871
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
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM crm_deals WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM crm_stage_history WHERE owner_id=$1", OWNER))


@pytest.fixture(autouse=True)
def _clean(pool):
    _wipe(pool)
    yield


def _contact(pool, name="Иван"):
    return _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1,$2) RETURNING id",
        OWNER, name))


def _hist(pool, kind, key):
    return [(r["from_stage"], r["to_stage"]) for r in _run(pool.fetch(
        "SELECT from_stage, to_stage FROM crm_stage_history "
        "WHERE owner_id=$1 AND deal_kind=$2 AND deal_key=$3 ORDER BY id",
        OWNER, kind, str(key)))]


def test_screen_stage_changes_are_recorded(pool):
    from services.contacts_hub.crm_engine import upsert_crm
    cid = _contact(pool)
    _run(upsert_crm(pool, OWNER, str(cid), {"stage": "lead"}))
    _run(upsert_crm(pool, OWNER, str(cid), {"stage": "proposal", "deal_value": 500}))
    _run(upsert_crm(pool, OWNER, str(cid), {"deal_value": 700}))  # не смена стадии
    _run(upsert_crm(pool, OWNER, str(cid), {"stage": "won"}))
    assert _hist(pool, "contact", cid) == [
        (None, "lead"), ("lead", "proposal"), ("proposal", "won")]


def test_simple_deal_changes_are_recorded(pool):
    did = _run(pool.fetchval(
        "INSERT INTO crm_deals (owner_id, title, stage, value) VALUES ($1,'Окна','lead',100) RETURNING id",
        OWNER))
    _run(pool.execute("UPDATE crm_deals SET stage='negotiation' WHERE id=$1", did))
    _run(pool.execute("UPDATE crm_deals SET notes='x' WHERE id=$1", did))
    assert _hist(pool, "deal", did) == [(None, "lead"), ("lead", "negotiation")]


def test_merged_deal_keeps_its_history(pool):
    from services.contacts_hub.crm_engine import upsert_crm
    from services.contacts_hub.merge_engine import manual_merge
    a, b = _contact(pool), _contact(pool)
    _run(upsert_crm(pool, OWNER, str(b), {"stage": "contact"}))
    _run(upsert_crm(pool, OWNER, str(b), {"stage": "negotiation"}))
    assert _run(manual_merge(pool, a, b, OWNER))["status"] == "merged"
    assert _hist(pool, "contact", a) == [
        (None, "contact"), ("contact", "negotiation")], "история сделки потеряна при слиянии"


def test_backfill_is_marked_and_not_doubled(pool):
    cid = _contact(pool)
    _run(pool.execute(
        "INSERT INTO contact_crm (owner_id, contact_id, stage) VALUES ($1,$2,'proposal')", OWNER, cid))
    _run(pool.execute("DELETE FROM crm_stage_history WHERE owner_id=$1", OWNER))
    sql = open(os.path.join(ROOT, "schema_v363_crm_stage_history.sql"), encoding="utf-8").read()
    _run(pool.execute(sql))
    _run(pool.execute(sql))
    rows = _run(pool.fetch(
        "SELECT to_stage, backfilled FROM crm_stage_history WHERE owner_id=$1", OWNER))
    assert [(r["to_stage"], r["backfilled"]) for r in rows] == [("proposal", True)]


def test_report_runs_against_the_real_schema(pool):
    from services import crm_funnel_analytics as fa
    from services.contacts_hub.crm_engine import upsert_crm
    a, b = _contact(pool, "Анна"), _contact(pool, "Борис")
    _run(upsert_crm(pool, OWNER, str(a), {"stage": "lead", "deal_value": 1000, "currency": "RUB"}))
    _run(upsert_crm(pool, OWNER, str(a), {"stage": "won"}))
    _run(upsert_crm(pool, OWNER, str(b), {"stage": "proposal", "deal_value": 50, "currency": "USD"}))
    _run(pool.execute(
        "INSERT INTO crm_deals (owner_id, title, stage, value) VALUES ($1,'Окна','lead',100)", OWNER))
    # удалённая сделка — не проигранная: в отчёт не попадает
    gone = _run(pool.fetchval(
        "INSERT INTO crm_deals (owner_id, title, stage) VALUES ($1,'Удалённая','lead') RETURNING id", OWNER))
    _run(pool.execute("DELETE FROM crm_deals WHERE id=$1", gone))
    rep = _run(fa.report(pool, OWNER, 90))
    assert rep["cohort"] == 3, rep
    assert rep["won"] == 1 and rep["open"] == 2
    assert "USD" in rep["pipeline_by_currency"]
    assert rep["history_since"] is not None
    assert all(s.get("label") for s in rep["stages"])
