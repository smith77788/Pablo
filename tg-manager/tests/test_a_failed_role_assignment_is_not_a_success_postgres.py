"""Назначение ролей в ноде, которое не удалось, не объявляется выполненным.

Исход операции считают по счётчикам, а не по слову исполнителя: это и есть
смысл `op_status.classify_final`. `_exec_community_set_staff` возвращал число
назначенных под ключом "promoted", а провалы не считал вовсе — `ok` остаётся
нулём (алиасы нормализатора слова "promoted" не знают), `failed` нулём тоже, и
прогон, в котором НИ ОДИН аккаунт не получил роль, приходил как честное
«выполнено».

Цена не только в неверной строчке у владельца. Предохранители продукта —
circuit breaker, пейсинг, детектор вспышек банов — судят по тем же
счётчикам. Промоут не проходит ровно тогда, когда у аккаунта-промоутера нет
прав или он под ограничением, то есть в момент, когда защита обязана
увидеть сбой. Вместо сбоя она видела успех.

Нужен живой Postgres: исполнитель читает ноду, её членов и аккаунты.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993437
CHAT_ID = -1009930000037
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
    _run(_cleanup(p))
    _run(p.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'roles_test') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_cleanup(p))
    _run(p.close())


async def _cleanup(p) -> None:
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM community_nodes WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


def _seed(pool, n_targets: int = 3) -> tuple[int, int, list[int]]:
    """Нода с аккаунтом-промоутером и n аккаунтами-кандидатами в штат."""
    _SEQ[0] += 1
    n = _SEQ[0]

    async def _mk():
        node_id = await pool.fetchval(
            "INSERT INTO community_nodes(owner_id, tg_chat_id, title) "
            "VALUES($1,$2,'нода') RETURNING id", OWNER, CHAT_ID - n)
        promoter = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, tg_user_id) "
            "VALUES($1,$2,'session',TRUE,$3) RETURNING id",
            OWNER, f"+7000{OWNER}{n}0", 500100 + n * 10)
        await pool.execute(
            "INSERT INTO community_node_members(node_id, account_id, role) "
            "VALUES($1,$2,'admin')", node_id, promoter)
        targets = []
        for i in range(n_targets):
            aid = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, tg_user_id) "
                "VALUES($1,$2,'session',TRUE,$3) RETURNING id",
                OWNER, f"+7000{OWNER}{n}{i + 1}", 500200 + n * 10 + i)
            targets.append(int(aid))
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
            "   label, started_at) "
            "VALUES($1,'community_set_staff','running',$2::jsonb,$3,'роли',now()) "
            "RETURNING id", OWNER, '{}', n_targets)
        return int(node_id), int(op_id), targets

    return _run(_mk())


def _verdict(res: dict) -> tuple[str, int, int]:
    """Исход так, как его посчитает воркер: по счётчикам результата."""
    from services import op_status
    from services.op_errors import _normalize_result

    norm = _normalize_result(dict(res), "community_set_staff", 1.0)
    return (
        op_status.classify_final(
            norm.get("status"), norm.get("ok"), norm.get("failed"),
            norm.get("ok"), norm.get("total")),
        int(norm.get("ok") or 0),
        int(norm.get("failed") or 0),
    )


def test_roles_that_were_all_refused_are_a_failure(pool, monkeypatch):
    """Ни одна роль не назначена — это провал, а не «выполнено»."""
    from services import account_manager, op_status, op_worker

    node_id, op_id, targets = _seed(pool, n_targets=3)

    async def _refuse(*a, **kw):
        # Так ведёт себя промоутер без права add_admins.
        raise RuntimeError("CHAT_ADMIN_REQUIRED")

    monkeypatch.setattr(account_manager, "promote_to_admin", _refuse)
    monkeypatch.setattr(op_worker, "_governed_sleep", _noop_sleep)
    op_worker._cancel_cache.pop(op_id, None)

    res = _run(op_worker._exec_community_set_staff(
        pool, None, op_id, OWNER,
        {"node_id": node_id, "role": "moderator", "account_ids": targets}))

    verdict, ok, failed = _verdict(res)
    assert verdict == op_status.FAILED, (
        f"исход прогона, где ни одна роль не назначена: {verdict} (ok={ok}, "
        f"failed={failed}) — владельцу докладывают успех, а предохранители "
        "продукта получают ложный сигнал успеха вместо сбоя прав")


def test_roles_assigned_in_part_are_partial(pool, monkeypatch):
    """Часть ролей назначена, часть нет — это PARTIAL."""
    from services import account_manager, op_status, op_worker

    node_id, op_id, targets = _seed(pool, n_targets=3)
    seen: list[int] = []

    async def _half(session, chat_id, uid_tg, **kw):
        seen.append(int(uid_tg))
        if len(seen) == 1:
            raise RuntimeError("USER_PRIVACY_RESTRICTED")
        return True

    monkeypatch.setattr(account_manager, "promote_to_admin", _half)
    monkeypatch.setattr(op_worker, "_governed_sleep", _noop_sleep)
    op_worker._cancel_cache.pop(op_id, None)

    res = _run(op_worker._exec_community_set_staff(
        pool, None, op_id, OWNER,
        {"node_id": node_id, "role": "admin", "account_ids": targets}))

    verdict, ok, failed = _verdict(res)
    assert verdict == op_status.PARTIAL, (
        f"исход частичного прогона: {verdict} (ok={ok}, failed={failed})")


async def _noop_sleep(*a, **kw):
    return None
