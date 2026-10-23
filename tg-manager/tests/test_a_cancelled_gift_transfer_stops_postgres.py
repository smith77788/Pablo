"""Отменённая передача подарков останавливается и остаётся отменённой.

Передача подарка необратима и тратит звёзды владельца, поэтому «Отменить» для
неё — самая дорогая кнопка продукта. Отмена доходит до исполнителей опросом:
в `op_worker` её спрашивают `_is_cancelled` на каждой цели (десятки вызовов).
`_exec_gift_transfer` не спрашивал её ни разу: нажатая отмена не прекращала
передачу, и подарки продолжали уходить получателю, пока не кончится список.

Вторая половина того же разрыва — финал. Все пять путей завершения операции
идут через одну дверь (`_op_closed_by_us`), и её UPDATE трогает строку только
если та ещё не терминальная: отмену владельца никто не переписывает. А этот
исполнитель писал `operation_queue.status='done'` сам, без условия, и
последним. То есть он затирал отмену словом «выполнено», а дверь после него
получала ноль строк и молчала — владельцу об исходе не докладывали вовсе.

Нужен живой Postgres: проверяется реальное содержимое строки операции после
прогона, а заглушка пула SQL не исполняет.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993431
PHONE = "+70000993431"

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


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
    _run(p.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'gift_test') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_cleanup(p))
    _run(p.close())


async def _cleanup(p) -> None:
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM gift_transfer_plans WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM gift_inventory WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE phone=$1", PHONE)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


def _seed(pool, n_items: int = 3) -> tuple[int, int]:
    """Создать план на n подарков и операцию в статусе 'running'. (plan_id, op_id)."""
    async def _mk():
        acc_id = await pool.fetchval(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND phone=$2", OWNER, PHONE)
        if not acc_id:
            acc_id = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1,$2,'session',TRUE) RETURNING id", OWNER, PHONE)
        plan_id = await pool.fetchval(
            "INSERT INTO gift_transfer_plans(owner_id, recipient_user_id, "
            "   payment_source, status, total_gifts, selected_gifts) "
            "VALUES($1,$2,'stars','queued',$3,$3) RETURNING id",
            OWNER, 777000111, n_items)
        base = 5000 + 100 * int(plan_id)
        for i in range(n_items):
            inv_id = await pool.fetchval(
                "INSERT INTO gift_inventory(owner_id, account_id, gift_id, gift_type, "
                "   stars_cost, is_transferable) VALUES($1,$2,$3,'Basic Star',25,TRUE) "
                "RETURNING id", OWNER, acc_id, str(base + i))
            await pool.execute(
                "INSERT INTO gift_transfer_items(plan_id, inventory_id, account_id, "
                "   gift_id, gift_type, stars_cost, status) "
                "VALUES($1,$2,$3,$4,'Basic Star',25,'pending')",
                plan_id, inv_id, acc_id, str(base + i))
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, "
            "   total_items, label, started_at) "
            "VALUES($1,'gift_transfer','running',$2::jsonb,$3,'подарки',now()) "
            "RETURNING id", OWNER, '{"plan_id": 0}', n_items)
        return int(plan_id), int(op_id)

    return _run(_mk())


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Передача подарка и отчёт — не предмет этого теста."""
    from services import gift_operation, gift_report

    async def _report(pool, plan_id, op_id):
        return 0

    monkeypatch.setattr(
        gift_report.GiftTransferReportService, "generate_report",
        staticmethod(_report), raising=False)
    yield gift_operation


def test_cancelling_a_gift_transfer_stops_sending_gifts(pool, monkeypatch):
    """Отмена посреди прогона прекращает передачу остальных подарков."""
    from services import gift_operation, op_worker

    plan_id, op_id = _seed(pool, n_items=3)
    sent: list[int] = []

    async def _fake_transfer(p, item, recipient_user_id, recipient_username):
        sent.append(int(item["id"]))
        # Владелец нажимает «Отменить» ровно после первого подарка.
        if len(sent) == 1:
            await p.execute(
                "UPDATE operation_queue SET status='cancelled', finished_at=now() "
                "WHERE id=$1", op_id)
            op_worker._cancel_cache.pop(op_id, None)
        return gift_operation.GiftTransferResult(
            item_id=int(item["id"]), success=True, cost=25)

    monkeypatch.setattr(gift_operation, "_transfer_single_gift", _fake_transfer)
    op_worker._cancel_cache.pop(op_id, None)

    res = _run(gift_operation._exec_gift_transfer(pool, op_id, {"plan_id": plan_id}))

    assert len(sent) == 1, (
        f"после отмены передано подарков: {len(sent)} — исполнитель не спрашивает "
        "отмену, и необратимые передачи продолжаются после нажатия «Отменить»")
    assert res.get("status") == "cancelled", f"исполнитель вернул: {res}"


def test_a_cancelled_gift_transfer_is_not_overwritten_with_done(pool, monkeypatch):
    """Исполнитель не переписывает терминальный статус строки операции."""
    from services import gift_operation, op_worker

    plan_id, op_id = _seed(pool, n_items=2)

    async def _fake_transfer(p, item, recipient_user_id, recipient_username):
        return gift_operation.GiftTransferResult(
            item_id=int(item["id"]), success=True, cost=25)

    monkeypatch.setattr(gift_operation, "_transfer_single_gift", _fake_transfer)
    _run(pool.execute(
        "UPDATE operation_queue SET status='cancelled', finished_at=now() WHERE id=$1",
        op_id))
    op_worker._cancel_cache.pop(op_id, None)

    _run(gift_operation._exec_gift_transfer(pool, op_id, {"plan_id": plan_id}))

    status = _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id))
    assert status == "cancelled", (
        f"статус отменённой операции стал '{status}': исполнитель закрыл строку "
        "сам и затёр отмену владельца — тому докладывают «выполнено» о том, что "
        "он остановил, а общая дверь завершения получает ноль строк и молчит")


def test_a_partly_failed_transfer_reports_honest_counters(pool, monkeypatch):
    """Часть подарков ушла, часть нет — это PARTIAL, а не провал и не успех."""
    from services import gift_operation, op_status

    plan_id, op_id = _seed(pool, n_items=3)
    seen: list[int] = []

    async def _fake_transfer(p, item, recipient_user_id, recipient_username):
        seen.append(int(item["id"]))
        if len(seen) == 1:
            return gift_operation.GiftTransferResult(
                item_id=int(item["id"]), success=False,
                error_message="Timeout during transfer", error_code="rate_limit")
        return gift_operation.GiftTransferResult(
            item_id=int(item["id"]), success=True, cost=25)

    monkeypatch.setattr(gift_operation, "_transfer_single_gift", _fake_transfer)

    res = _run(gift_operation._exec_gift_transfer(pool, op_id, {"plan_id": plan_id}))

    from services.op_errors import _normalize_result

    norm = _normalize_result(dict(res), "gift_transfer", 1.0)
    verdict = op_status.classify_final(
        norm.get("status"), norm.get("ok"), norm.get("failed"),
        norm.get("ok"), norm.get("total"))
    assert verdict == op_status.PARTIAL, (
        f"исход частично удавшейся передачи: {verdict} (ok={norm.get('ok')}, "
        f"failed={norm.get('failed')}) — ключ 'transferred' в число успешных целей "
        "не переводится, поэтому две ушедшие передачи из трёх объявляются провалом")

    row = _run(pool.fetchrow(
        "SELECT status, done_items, total_items FROM operation_queue WHERE id=$1", op_id))
    assert row["status"] == "running", (
        f"исполнитель сам закрыл операцию как '{row['status']}': статус финала "
        "ставит одна дверь, иначе честный исход до строки не доходит")
    assert int(row["done_items"]) == 3, (
        f"done_items={row['done_items']} при трёх разобранных подарках: прогресс "
        "операции не виден владельцу, пока она идёт")
