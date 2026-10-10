"""Повтор и закрытие по dead letter — по НАСТОЯЩЕМУ Postgres.

Здесь живут запросы, которые нельзя проверить заглушкой пула: она принимает
любые типы параметров и любые имена колонок, поэтому ошибку СВЯЗЫВАНИЯ не видит
в принципе — ровно так пережил релиз сломанный `submit(scheduled_for=…)`,
убивавший все отложенные операции продукта. И ровно так же завышался прогресс
операции служебными строками журнала: на заглушке 21 из 20 выглядело нормально.

Что проверяется на живой базе:

  * `recovery_engine._dead_letter_op` — закрытие зависшей операции, исчерпавшей
    повторы: статус параметром, `RETURNING` с `EXTRACT(EPOCH …)`, `result`
    jsonb-ом, снятые часы ожидания флота;
  * `operation_bus.closed_targets_count` — `op_id = ANY($1::bigint[])` плюс
    `count(DISTINCT target)` по всей семье повторов;
  * `operation_bus.resubmit_one` — размер повтора как ОСТАТОК работы, ссылка на
    журнал предка, выброшенное расписание;
  * `operation_bus.resubmit_unfinished` — выборка по `status IN ('failed',
    'partial')` и окну `$2 * INTERVAL '1 hour'`;
  * `recovery_engine._operation_recovery` — эскалация по тому же списку статусов
    вместе с `op_status.sql_error_reason()`.

КАК ЗАПУСТИТЬ — рецепт в докстринге tests/test_op_finish_paths_e2e_postgres.py.
Без INFRAGRAM_TEST_DSN файл пропускается.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990991

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

    async def _boot():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    try:
        _run(p.fetchval("SELECT 1 FROM operation_queue LIMIT 1"))
    except Exception as exc:
        pytest.skip(f"схема не накатана (запустите соседний e2e-файл): {str(exc)[:80]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def clean(pool):
    # Операции продукта закрыты тарифом (OP_REGISTRY[op]["min_plan"]), и шина
    # проверяет его ПЕРЕД постановкой. Без подписки повтор честно отказывал бы
    # PlanRequiredError — на живой базе это видно сразу, на заглушке не видно
    # вообще.
    _run(pool.execute(
        "INSERT INTO subscriptions(user_id, plan, is_active, expires_at) "
        "VALUES($1,'pro',true, now() + interval '30 days') "
        "ON CONFLICT DO NOTHING", OWNER))
    _run(pool.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER))
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    yield


async def _op(pool, *, status="running", op_type="mass_invite", params=None,
              total=380, done=203, retry=3, age_min=10) -> int:
    return await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, "
        "       total_items, done_items, retry_count, max_retries, label, "
        "       started_at, created_at, acct_wait_since) "
        "VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,3,'Приглашение', "
        "       now() - make_interval(mins => $8), "
        "       now() - make_interval(mins => $8), "
        "       now() - interval '90 minutes') RETURNING id",
        OWNER, op_type, status, json.dumps(params or {}), total, done, retry,
        age_min)


async def _journal(pool, op_id, n_ok, *, prefix="u", status="ok", start=0):
    for i in range(start, start + n_ok):
        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status) "
            "VALUES($1,$2,$3,$4)", op_id, i, f"{prefix}{i}", status)


def test_dead_letter_closes_with_an_honest_status(pool):
    from services import recovery_engine

    op_id = _run(_op(pool))
    _run(_journal(pool, op_id, 203))

    closed = _run(recovery_engine._dead_letter_op(pool, OWNER, op_id, 420, 3, 3))
    assert closed, "строка не закрыта"
    status, msg = closed

    row = _run(pool.fetchrow(
        "SELECT status, error_msg, last_error, acct_wait_since, "
        "       finished_at IS NOT NULL AS finished, result "
        "FROM operation_queue WHERE id=$1", op_id))
    assert row["status"] == "partial" == status, (
        f"операция сделала 203 цели из 380 и закрыта как {row['status']!r}")
    assert row["finished"] and row["acct_wait_since"] is None
    assert row["error_msg"] == msg and row["last_error"] == msg
    assert "мин" in msg and "повтор" in msg.lower(), msg
    res = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
    assert res and res.get("ok") == 203 and res.get("status") == "partial", res


def test_dead_letter_does_not_touch_a_row_someone_else_closed(pool):
    from services import recovery_engine

    op_id = _run(_op(pool, status="cancelled"))
    assert _run(recovery_engine._dead_letter_op(pool, OWNER, op_id, 60, 3, 3)) is None
    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)) == "cancelled"


def test_closed_targets_counts_the_whole_family(pool):
    from services import operation_bus as obus

    parent = _run(_op(pool, status="partial"))
    _run(_journal(pool, parent, 203))
    child = _run(_op(pool, status="partial", params={"retry_of_op": parent}))
    _run(_journal(pool, child, 100, start=203))
    # повтор той же цели ошибкой закрытой не делает
    _run(_journal(pool, child, 1, start=0, status="error"))

    assert _run(obus.closed_targets_count(pool, child)) == 303, (
        "повтор не видит работу предка — размер остатка будет завышен")


def test_retry_is_sized_by_the_remaining_work(pool):
    from services import operation_bus as obus

    # find_contact — тип БЕЗ точечного повтора по целям, то есть путь «операция
    # целиком», где и считается остаток.
    op_id = _run(_op(pool, status="partial", op_type="find_contact",
                     params={"targets": ["a"], "repeat_interval_min": 60}))
    _run(_journal(pool, op_id, 203))

    res = _run(obus.resubmit_one(pool, OWNER, op_id))
    assert res["ok"], res
    new_row = _run(pool.fetchrow(
        "SELECT total_items, params, status FROM operation_queue WHERE id=$1",
        res["op_id"]))
    assert new_row["total_items"] == 177, (
        f"повтор встал на размер предка, а не на остаток: {new_row['total_items']}")
    params = (json.loads(new_row["params"]) if isinstance(new_row["params"], str)
              else new_row["params"])
    assert params.get("retry_of_op") == op_id
    assert "repeat_interval_min" not in params, "повтор склонировал расписание"
    assert new_row["status"] == "pending"


def test_retry_refuses_when_the_journal_covers_everything(pool):
    from services import operation_bus as obus

    op_id = _run(_op(pool, status="partial", op_type="find_contact", total=50,
                     params={"targets": ["a"]}))
    _run(_journal(pool, op_id, 50))
    res = _run(obus.resubmit_one(pool, OWNER, op_id))
    assert res["ok"] is False and "закрыты" in res["reason"], res


def test_bulk_retry_takes_failed_and_partial_only(pool):
    from services import operation_bus as obus

    ids = {
        "partial": _run(_op(pool, status="partial", op_type="bulk_join")),
        "failed": _run(_op(pool, status="failed", op_type="bulk_join")),
        "done": _run(_op(pool, status="done", op_type="bulk_join")),
        "cancelled": _run(_op(pool, status="cancelled", op_type="bulk_join")),
        "publish": _run(_op(pool, status="failed", op_type="mass_publish")),
        "old": _run(_op(pool, status="failed", op_type="bulk_join",
                        age_min=60 * 48)),
    }
    res = _run(obus.resubmit_unfinished(pool, OWNER, hours=24))
    assert (res["retried"], res["skipped"]) == (2, 1), (res, ids)

    fresh = _run(pool.fetch(
        "SELECT params FROM operation_queue WHERE owner_id=$1 AND status='pending'",
        OWNER))
    parents = set()
    for r in fresh:
        p = json.loads(r["params"]) if isinstance(r["params"], str) else r["params"]
        parents.add(p.get("retry_of_op"))
    assert parents == {ids["partial"], ids["failed"]}, parents


def test_escalation_sees_unfinished_operations(pool):
    from services import recovery_engine

    op_id = _run(_op(pool, status="partial"))
    _run(pool.execute(
        "UPDATE operation_queue SET finished_at=now() - interval '5 minutes', "
        "       notified_at=NULL, error_msg='часть целей не взята' WHERE id=$1",
        op_id))
    actions = _run(recovery_engine._operation_recovery(pool, None, OWNER))
    assert [a.target_id for a in actions] == [op_id], (
        "недоведённая операция, исчерпавшая повторы, не эскалирована")
