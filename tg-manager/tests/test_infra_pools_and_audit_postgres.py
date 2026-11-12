"""Пулы и журнал аудита — на живой базе.

Заглушка пула не проверяет ни типы параметров, ни сам SQL: `make_interval`,
`ANY($n::text[])`, `IS DISTINCT FROM` и сводка через FILTER проверяются
только настоящим Postgres. Нужна INFRAGRAM_TEST_DSN (см. tests/conftest.py).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import infra_hub as ih

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993771
STRANGER = 993772

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p) -> None:
    for uid in (OWNER, STRANGER):
        await p.execute("DELETE FROM operation_audit WHERE owner_id=$1", uid)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", uid)
        await p.execute("DELETE FROM platform_users WHERE user_id=$1", uid)


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
    for uid in (OWNER, STRANGER):
        _run(p.execute("INSERT INTO platform_users(user_id, username) VALUES($1,'infra_t') "
                       "ON CONFLICT (user_id) DO NOTHING", uid))
    yield p
    _run(_cleanup(p))
    _run(p.close())


def _acc(p, owner, phone, **kw):
    async def _mk():
        return await p.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, trust_score, "
            "  pool, acc_status, cooldown_until) "
            "VALUES($1,$2,'s',$3,$4,$5,$6,$7) RETURNING id",
            owner, phone, kw.get("active", True), kw.get("trust", 0.5), kw.get("pool"),
            kw.get("status", "active"), kw.get("cooldown"))
    return _run(_mk())


def test_pools_lifecycle_on_a_real_database(pool):
    from datetime import datetime, timedelta, timezone

    a1 = _acc(pool, OWNER, "+7000993771001", trust=0.9)
    a2 = _acc(pool, OWNER, "+7000993771002", trust=0.1)
    a3 = _acc(pool, OWNER, "+7000993771003", trust=0.5,
              cooldown=datetime.now(timezone.utc) + timedelta(hours=1))
    a4 = _acc(pool, OWNER, "+7000993771004", trust=0.5, status="banned", pool="old")
    foreign = _acc(pool, STRANGER, "+7000993772001", pool="old")

    # Создать пул из всех без пула.
    assert _run(ih.assign_pool(pool, OWNER, "Инвайт", only_unpooled=True)) == 3
    ov = _run(ih.pools_overview(pool, OWNER))
    by = {p["pool"]: p for p in ov["pools"]}
    assert by["Инвайт"]["total"] == 3 and by["Инвайт"]["cooldown"] == 1
    assert by["old"]["total"] == 1 and by["old"]["dead"] == 1 and by["old"]["alive"] == 0
    assert ov["unpooled"]["total"] == 0

    # Чужой id в выборе не трогается: скоуп — запросом.
    assert _run(ih.assign_pool(pool, OWNER, "x", account_ids=[foreign])) == 0
    # Переименование в существующее имя сливает пулы; чужой «old» цел.
    assert _run(ih.rename_pool(pool, OWNER, "old", "Инвайт")) == 1
    assert _run(pool.fetchval("SELECT pool FROM tg_accounts WHERE id=$1", foreign)) == "old"
    # Автораспределение: план без записи, затем запись.
    plan = {c["id"]: c["to_pool"] for c in _run(ih.rebalance_plan(pool, OWNER))}
    assert plan == {a1: "primary", a2: "monitoring", a3: "cooldown"}
    res = _run(ih.rebalance_apply(pool, OWNER))
    assert res["changed"] == 3
    assert _run(ih.rebalance_plan(pool, OWNER)) == []
    # Роспуск.
    assert _run(ih.dissolve_pool(pool, OWNER, "Инвайт")) == 1
    assert _run(pool.fetchval("SELECT pool FROM tg_accounts WHERE id=$1", a4)) is None
    assert _run(pool.fetchval("SELECT pool FROM tg_accounts WHERE id=$1", foreign)) == "old"


def test_audit_page_and_summary_on_a_real_database(pool):
    acc = _acc(pool, OWNER, "+7000993771010", pool="P1")

    async def _seed():
        rows = [("warmup", "day 8", "error", "all 3 actions failed"),
                ("warmup", "day 9", "success", None),
                ("join", "@chat", "flood_wait", "A wait of 60 seconds is required"),
                ("join", "@chat2", "error", "USER_BANNED_IN_CHANNEL")]
        for action, target, result, err in rows:
            await pool.execute(
                "INSERT INTO operation_audit(owner_id, account_id, action, target, result, error_msg) "
                "VALUES($1,$2,$3,$4,$5,$6)", OWNER, acc, action, target, result, err)
        await pool.execute(
            "INSERT INTO operation_audit(owner_id, action, result) VALUES($1,'join','error')",
            STRANGER)
    _run(_seed())

    page = _run(ih.audit_page(pool, OWNER, period="24h"))
    assert page["total"] == 4 and len(page["rows"]) == 4
    assert {r["action_ru"] for r in page["rows"]} == {"Прогрев аккаунта", "Вступление в чат"}
    fail = _run(ih.audit_page(pool, OWNER, result="fail", period="7d"))
    assert fail["total"] == 2
    warn = _run(ih.audit_page(pool, OWNER, result="warn"))
    assert warn["total"] == 1 and "подождать 60" in warn["rows"][0]["error_ru"]
    assert _run(ih.audit_page(pool, OWNER, action="warmup"))["total"] == 2
    assert _run(ih.audit_page(pool, OWNER, pool_name="P1"))["total"] == 4
    assert _run(ih.audit_page(pool, OWNER, q="chat2"))["total"] == 1
    assert _run(ih.audit_page(pool, OWNER, limit=1, offset=1))["rows"][0]["id"]

    s = _run(ih.audit_summary(pool, OWNER, "7d"))
    assert (s["total"], s["ok"], s["warn"], s["fail"]) == (4, 1, 1, 2)
    reasons = {g["reason"] for g in s["top_errors"]}
    assert "Не удалось ни одно из 3 действий" in reasons
    assert "Аккаунту запрещено писать в этом чате" in reasons
