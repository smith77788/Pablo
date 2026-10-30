"""Отмена плана присутствия закрывает его операцию ПО-НАСТОЯЩЕМУ.

Зачем живая база. `cancel_global_presence_plan` переведена с собственного
UPDATE на дверь шины (`operation_bus.cancel`), и вместе с дверью поменялись
границы транзакции: операция гасится ДО транзакции плана, а не внутри неё.
Стенд проверяет маршрут — что дверь вызвана, — но не проверяет ни SQL, ни
порядок, ни то, что у операции в самом деле появились статус, время
завершения и ЦИФРЫ. Именно цифры и были потеряны прежней правкой: владелец
видел «Отменено» без единого числа.

Порядок «операция, потом план» выбран не произвольно: если дверь не
сработает, план останется живым и состояние не разъедется наполовину, а
обратный порядок чинится сам (`sync_plan_status_from_op`). Тест закрепляет и
это: при сорванной отмене план НЕ помечается отменённым.
"""
from __future__ import annotations

import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"),
]

OWNER = 990_777


async def _pool():
    import asyncpg
    return await asyncpg.create_pool(DSN, min_size=1, max_size=3)


async def _make_plan_with_op(pool, op_status_value: str = "pending"):
    """План с привязанной операцией и одной взятой целью в журнале."""
    op_id = await pool.fetchval(
        """INSERT INTO operation_queue(owner_id, op_type, params, status,
                                       total_items, done_items)
           VALUES($1,'global_presence_channel','{}'::jsonb,$2,5,2)
        RETURNING id""",
        OWNER, op_status_value)
    # Две отметки в журнале: по ним дверь посчитает цифры для владельца.
    for n, st in ((1, "ok"), (2, "ok")):
        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status) "
            "VALUES($1,$2,$3,$4)", op_id, n, f"t{n}", st)
    plan_id = await pool.fetchval(
        """INSERT INTO global_presence_plans(owner_id, name_pattern, status, op_id)
           VALUES($1,'{city}','running',$2) RETURNING id""",
        OWNER, op_id)
    await pool.execute(
        """INSERT INTO global_presence_targets(plan_id, asset_type, status)
           VALUES($1,'channel','pending'),($1,'channel','done')""",
        plan_id)
    return plan_id, op_id


async def _cleanup(pool, plan_id, op_id):
    await pool.execute("DELETE FROM global_presence_targets WHERE plan_id=$1", plan_id)
    await pool.execute("DELETE FROM global_presence_plans WHERE id=$1", plan_id)
    await pool.execute("DELETE FROM operation_log WHERE op_id=$1", op_id)
    await pool.execute("DELETE FROM operation_queue WHERE id=$1", op_id)


async def test_the_operation_gets_a_verdict_with_figures():
    from database import db

    pool = await _pool()
    plan_id, op_id = await _make_plan_with_op(pool)
    try:
        ok = await db.cancel_global_presence_plan(pool, plan_id, OWNER)
        assert ok is True

        row = await pool.fetchrow(
            "SELECT status, finished_at, result FROM operation_queue WHERE id=$1",
            op_id)
        assert row["status"] == "cancelled", (
            f"операция не отменена вместе с планом: {row['status']}")
        assert row["finished_at"] is not None, (
            "у отменённой операции нет времени завершения — «сколько шла» "
            "и «когда закончилась» снова никто не ответит")
        assert row["result"] is not None, (
            "итог пустой: владелец увидит «Отменено» без единой цифры, "
            "хотя часть целей отработана — ровно та потеря, из-за которой "
            "путь и переведён на дверь шины")
        res = row["result"]
        if isinstance(res, str):
            res = json.loads(res)
        assert int(res.get("ok") or 0) == 2, (
            f"в итоге не те цифры, что в журнале целей: {res}")

        plan = await pool.fetchrow(
            "SELECT status FROM global_presence_plans WHERE id=$1", plan_id)
        assert plan["status"] == "cancelled"
        left = await pool.fetchval(
            "SELECT count(*) FROM global_presence_targets "
            "WHERE plan_id=$1 AND status='pending'", plan_id)
        assert left == 0, "незапущенные цели плана остались ожидающими"
        done = await pool.fetchval(
            "SELECT count(*) FROM global_presence_targets "
            "WHERE plan_id=$1 AND status='done'", plan_id)
        assert done == 1, "отмена плана переписала уже сделанную цель"
    finally:
        await _cleanup(pool, plan_id, op_id)
        await pool.close()


async def test_a_finished_operation_keeps_its_own_verdict():
    """Отмена плана не вправе переписать исход уже отработавшей операции."""
    from database import db

    pool = await _pool()
    plan_id, op_id = await _make_plan_with_op(pool, op_status_value="partial")
    try:
        await db.cancel_global_presence_plan(pool, plan_id, OWNER)
        got = await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op_id)
        assert got == "partial", (
            f"исход завершённой операции переписан отменой плана: {got}")
    finally:
        await _cleanup(pool, plan_id, op_id)
        await pool.close()


async def test_a_plan_of_another_owner_is_untouched():
    from database import db

    pool = await _pool()
    plan_id, op_id = await _make_plan_with_op(pool)
    try:
        ok = await db.cancel_global_presence_plan(pool, plan_id, OWNER + 1)
        assert ok is False, "план чужого владельца отменён"
        st = await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op_id)
        assert st == "pending", f"операция чужого плана тронута: {st}"
    finally:
        await _cleanup(pool, plan_id, op_id)
        await pool.close()
