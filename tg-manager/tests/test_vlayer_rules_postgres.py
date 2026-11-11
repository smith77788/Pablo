"""Правила модели, ручная ступень и аналитика за период — на живом Postgres.

Заглушка пула не проверяет ни jsonb, ни `make_interval` с параметром, ни
`percentile_cont ... FILTER`: проверка текста запроса подтверждает форму, а не
то, что база его примет (CLAUDE.md). Здесь запросы идут в настоящую схему.

Без INFRAGRAM_TEST_DSN — скип.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 330981


async def _cleanup(conn):
    await conn.execute("DELETE FROM virtual_state_history WHERE owner_id=$1", OWNER)
    await conn.execute("DELETE FROM virtual_states WHERE owner_id=$1", OWNER)
    await conn.execute("DELETE FROM vlayer_rules WHERE owner_id=$1", OWNER)


async def _hist(conn, eid, frm, to, reason, hours_ago):
    await conn.execute(
        "INSERT INTO virtual_state_history(owner_id, entity_type, entity_id, "
        "state_key, from_value, to_value, reason, confidence, created_at) "
        "VALUES($1,'user',$2,'funnel',$3,$4,$5,0.7, now() - ($6::float * interval '1 hour'))",
        OWNER, eid, frm, to, reason, hours_ago)


@pytest.mark.asyncio
async def test_rules_roundtrip_and_signal_follows_them():
    from services import virtual_layer as V

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        assert await V.get_rules(conn, OWNER) == V.normalize_rules(None)
        saved = await V.save_rules(conn, OWNER, {
            "signal_target": {"asked_price": "ready", "unsubscribed": "off"},
            "ttl_hours": {"ready": 3}})
        got = await V.get_rules(conn, OWNER)
        assert got == saved
        assert got["signal_target"]["asked_price"] == "ready"

        ch = await V.signal(conn, OWNER, V.USER, "p1", "asked_price")
        assert ch["value"] == "ready"
        row = await conn.fetchrow(
            "SELECT value, expires_at - updated_at AS ttl FROM virtual_states "
            "WHERE owner_id=$1 AND entity_id='p1' AND state_key='funnel'", OWNER)
        assert row["value"] == "ready"
        assert 2.9 * 3600 < row["ttl"].total_seconds() < 3.1 * 3600
        # «Отписался» выключен — человек не теряется.
        assert await V.signal(conn, OWNER, V.USER, "p1", "unsubscribed") is None

        assert await V.reset_rules(conn, OWNER) == V.normalize_rules(None)
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM vlayer_rules WHERE owner_id=$1", OWNER) == 0
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_set_stage_moves_down_and_writes_history():
    from services import virtual_layer as V

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        await V.signal(conn, OWNER, V.USER, "p2", "asked_how_to_pay")
        ch = await V.set_stage(conn, OWNER, V.USER, "p2", "curious")
        assert ch["value"] == "curious"
        assert await V.set_stage(conn, OWNER, V.USER, "p2", "curious") is None
        await V.set_stage(conn, OWNER, V.USER, "p2", V.LOST)
        st = await V.get_state(conn, OWNER, V.USER, "p2")
        assert st["value"] == V.LOST and st["source"] == "owner"
        # Из «Потерян» сигнал не вытаскивает, а владелец — может.
        await V.set_stage(conn, OWNER, V.USER, "p2", "interested")
        assert (await V.get_state(conn, OWNER, V.USER, "p2"))["value"] == "interested"
        reasons = [r["reason"] for r in await conn.fetch(
            "SELECT reason FROM virtual_state_history WHERE owner_id=$1 "
            "AND entity_id='p2' ORDER BY created_at, id", OWNER)]
        assert reasons.count("manual") == 3
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_analytics_period_time_to_leaks_drivers():
    from services import virtual_layer as V

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        # a: 10 ч до «готов», 20 ч до покупки — всё в последние сутки-двое.
        await _hist(conn, "a", None, "curious", "signal:opened", 30)
        await _hist(conn, "a", "curious", "ready", "signal:asked_how_to_pay", 20)
        await _hist(conn, "a", "ready", "purchased", "signal:paid", 10)
        # b: давно (60 дней назад) дошёл до «интерес» и остыл.
        await _hist(conn, "b", None, "interested", "signal:replied", 24 * 60)
        await _hist(conn, "b", "interested", "curious", "decay", 24 * 59)
        # c: ушёл в потерянные с «квалифицирован» вчера.
        await _hist(conn, "c", None, "qualified", "signal:clicked_offer", 40)
        await _hist(conn, "c", "qualified", "lost", "signal:refused", 20)

        all_time = await V.analytics(conn, OWNER, days=None)
        month = await V.analytics(conn, OWNER, days=30)
        assert all_time["days"] is None and month["days"] == 30

        reached = {c["value"]: c["reached"] for c in all_time["conversion"]}
        assert reached["curious"] == 3 and reached["purchased"] == 1
        reached_m = {c["value"]: c["reached"] for c in month["conversion"]}
        assert reached_m["curious"] == 2          # «b» за пределами месяца

        tt = month["time_to"]
        assert tt["ready_n"] == 1 and abs(tt["ready_hours"] - 10) < 0.2
        assert tt["purchased_n"] == 1 and abs(tt["purchased_hours"] - 20) < 0.2

        leaks_all = {x["value"]: x for x in all_time["leaks"]}
        assert leaks_all["interested"]["cooled"] == 1
        assert leaks_all["qualified"]["lost"] == 1
        assert "interested" not in {x["value"] for x in month["leaks"]}

        drivers = {d["reason"]: d["count"] for d in month["drivers"]}
        assert drivers.get("signal:paid") == 1
        assert "signal:refused" not in drivers     # уход в потерю — не движение вверх
        assert month["momentum"]["window_days"] == 30
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_overview_cooling_window_from_rules():
    from services import virtual_layer as V

    conn = await asyncpg.connect(_DSN)
    try:
        await _cleanup(conn)
        await conn.execute(
            "INSERT INTO virtual_states(owner_id, entity_type, entity_id, state_key, "
            "value, confidence, expires_at) VALUES "
            "($1,'user','soon','funnel','ready',0.8, now() + interval '10 hours'), "
            "($1,'user','later','funnel','ready',0.8, now() + interval '40 hours')",
            OWNER)
        ov = await V.overview(conn, OWNER)
        assert {c["entity_id"] for c in ov["cooling"]} == {"soon"}
        await V.save_rules(conn, OWNER, {"cooling_window_hours": 48})
        ov = await V.overview(conn, OWNER)
        assert {c["entity_id"] for c in ov["cooling"]} == {"soon", "later"}
        assert ov["cooling_window_hours"] == 48
    finally:
        await _cleanup(conn)
        await conn.close()
