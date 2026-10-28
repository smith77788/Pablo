"""Прерванный страйк не жалуется второй раз с тех же аккаунтов.

Страйк идёт волнами с паузами 2–5 минут между ними, то есть десятками минут
на цель. За это окно операция возвращается в очередь штатно: деплой
(`op_worker.shutdown`) или автоматический повтор по флуд-ошибке
(`max_retries: 1` у этого типа в реестре шины). Исполнитель начинал заново с
первой волны, и аккаунты, уже отправившие жалобу, отправляли её снова.

Повторная жалоба с одного аккаунта по одной цели — злоупотребление
репортом, то есть прямой риск для аккаунта, а страйк и так самый
рискованный вектор продукта. Анти-детект-гейт `is_strike_allowed` этот
случай НЕ закрывает: он смотрит в strike_history, а прерванный прогон до
записи истории не доходит.

Проверяется ровно прерывание: первый прогон отмечает два аккаунта и падает
(как падает прогон на деплое), второй обязан взять в волны только
оставшиеся. Нужен живой Postgres: отметки живут в operation_log.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993453
TARGET = "@strike_probe_target"
_SEQ = [0]

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
    _run(_clean(p))
    _run(p.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'strike_test') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_clean(p))
    _run(p.close())


async def _clean(p) -> None:
    await p.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM strike_history WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


def _seed(pool, n_accounts: int = 4):
    _SEQ[0] += 1
    k = _SEQ[0]

    async def _mk():
        acc_ids = []
        for i in range(n_accounts):
            aid = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, "
                "   trust_score, tg_user_id) "
                "VALUES($1,$2,'session',TRUE,0.9,$3) RETURNING id",
                OWNER, f"+7003{OWNER}{k}{i}", 600100 + k * 10 + i)
            acc_ids.append(int(aid))
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
            "   label, started_at) "
            "VALUES($1,'strike','running','{}'::jsonb,$2,'страйк',now()) RETURNING id",
            OWNER, n_accounts)
        return [int(a) for a in acc_ids], int(op_id)

    return _run(_mk())


def test_accounts_that_already_reported_are_out_of_the_next_run(pool, monkeypatch):
    """Второй прогон той же операции берёт только неотработавшие аккаунты."""
    from services import op_worker, strike_engine

    acc_ids, op_id = _seed(pool, n_accounts=4)
    params = {"target": TARGET, "reason": "spam", "num_waves": 2, "force": True,
              "account_ids": acc_ids}
    seen_plans: list[list[int]] = []
    got_journal_cb: list[bool] = []

    async def _interrupted(plan, progress_cb=None, pool=None, op_id=None, journal_cb=None):
        """Первый прогон: два аккаунта отработали, затем прогон оборвался."""
        seen_plans.append([int(a["id"]) for a in plan.accounts])
        got_journal_cb.append(journal_cb is not None)
        if journal_cb:
            for acc in plan.accounts[:2]:
                await journal_cb(int(acc["id"]))
        raise RuntimeError("воркер остановлен на рестарте")

    monkeypatch.setattr(strike_engine, "staggered_strike", _interrupted)
    res1 = _run(op_worker._exec_strike(pool, None, op_id, OWNER, params))
    assert res1.get("status") == "failed", f"первый прогон: {res1}"
    assert seen_plans and len(seen_plans[0]) == 4, f"в волны пошло: {seen_plans}"
    assert got_journal_cb and got_journal_cb[0], (
        "исполнителю страйка не передан журнал отработавших аккаунтов — "
        "прерванный прогон не оставляет следа, и повтор жалуется теми же "
        "аккаунтами второй раз")

    struck = seen_plans[0][:2]

    async def _second(plan, progress_cb=None, pool=None, op_id=None, journal_cb=None):
        seen_plans.append([int(a["id"]) for a in plan.accounts])
        return []

    monkeypatch.setattr(strike_engine, "staggered_strike", _second)
    _run(pool.execute(
        "UPDATE operation_queue SET status='running', done_items=0 WHERE id=$1", op_id))
    op_worker._cancel_cache.pop(op_id, None)

    _run(op_worker._exec_strike(pool, None, op_id, OWNER, params))

    assert len(seen_plans) == 2, "второй прогон не дошёл до волн"
    again = [a for a in seen_plans[1] if a in struck]
    assert not again, (
        f"аккаунты {again} идут в волны второй раз: они уже отправили жалобу по "
        "этой цели в прерванном прогоне, а повторная жалоба с одного аккаунта — "
        "злоупотребление репортом и риск для аккаунта")
    assert sorted(seen_plans[1]) == sorted(a for a in acc_ids if a not in struck), (
        f"во второй прогон пошли {seen_plans[1]}, ожидались оставшиеся "
        f"{[a for a in acc_ids if a not in struck]}")
