"""Уборка не вынимает журнал предка из-под работающего повтора.

Идемпотентность повтора стоит на журнале ПРЕДКА: у повтора свой op_id и
пустой журнал, а «что уже сделано» он читает по ссылке `retry_of_op`
(`operation_bus.retry_family_ids`). Статус у предка при этом терминальный —
и уборка журнала судила только по его возрасту.

Сроки сходятся вплотную: журнал хранится 30 суток, предельный срок жизни
одной операции — столько же, а кампания на 20 тысяч получателей при
безопасном темпе идёт НЕДЕЛЯМИ, продолжаясь через потолок прогона. Повторённая
владельцем один раз, она доживает до уборки журнала предка — и после этого
пишет людям, которым предок уже написал, и зовёт в группу тех, кого уже звал.
Это худший исход продукта: дубль реального действия и риск бана флота.

`resubmit_one` отказывает в повторе по ВСЕЙ семье (`journal_pruned` у любого
предка) — уборка обязана смотреть так же. Нужен живой Postgres: проверяется
сам запрос уборки.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993462

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
        "INSERT INTO platform_users(user_id, username) VALUES($1,'prune_test') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_clean(p))
    _run(p.close())


async def _clean(p) -> None:
    await p.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


def _family(pool, child_status: str):
    """Предок, закрытый 40 суток назад, и его повтор в заданном статусе."""
    async def _mk():
        parent = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, "
            "   created_at, finished_at, journal_pruned) "
            "VALUES($1,'mass_invite','partial','{}'::jsonb, "
            "        now() - interval '45 days', now() - interval '40 days', FALSE) "
            "RETURNING id", OWNER)
        for i in range(3):
            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                "VALUES($1,$2,$3,'ok','приглашён')", parent, i, f"@user{i}")
        child = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, created_at) "
            "VALUES($1,'mass_invite',$2,$3::jsonb, now() - interval '39 days') "
            "RETURNING id",
            OWNER, child_status, f'{{"retry_of_op": {int(parent)}}}')
        return int(parent), int(child)

    return _run(_mk())


def _prune(pool):
    from services import db_maintenance

    return _run(db_maintenance._prune_operation_log(pool))


def _journal_rows(pool, op_id: int) -> int:
    return int(_run(pool.fetchval(
        "SELECT count(*) FROM operation_log WHERE op_id=$1", op_id)) or 0)


def test_the_journal_survives_while_the_retry_is_still_working(pool):
    """Повтор в работе — журнал предка остаётся на месте."""
    parent, child = _family(pool, "running")

    deleted, err = _prune(pool)

    assert err is None, f"уборка отдала ошибку: {err}"
    assert _journal_rows(pool, parent) == 3, (
        f"журнал предка убран (удалено строк: {deleted}), хотя повтор #{child} ещё "
        "работает: дальше он позовёт тех же людей второй раз")
    pruned = _run(pool.fetchval(
        "SELECT journal_pruned FROM operation_queue WHERE id=$1", parent))
    assert not pruned, (
        "предок помечен journal_pruned при живом повторе — повтор владельца "
        "будет запрещён, хотя журнал был цел")


def test_the_journal_is_pruned_once_the_retry_is_over(pool):
    """Повтор завершён — журнал предка уборке не нужен и уходит."""
    parent, _child = _family(pool, "done")

    deleted, err = _prune(pool)

    assert err is None, f"уборка отдала ошибку: {err}"
    assert _journal_rows(pool, parent) == 0, (
        f"журнал завершённой семьи не убран (удалено строк: {deleted}) — уборка "
        "перестала работать вообще, а не стала аккуратнее")
