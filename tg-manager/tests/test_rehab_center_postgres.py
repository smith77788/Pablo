"""Центр восстановления на живом Postgres: запросы принимает настоящая база.

Главный дефект здесь — повторный блок. Аккаунт, которого однажды вернули
(фаза 'freed'), при новом спам-блоке не подхватывался НИКОГДА: заведение
делало ON CONFLICT DO NOTHING, а цикл берёт только активные фазы. Аккаунт
молча висел под блоком, хотя модуль обещал «подхватит как обычно».
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 996301
OTHER = 996302
_LOOP = None


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
        return await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    p = _run(_mk())
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    async def _c():
        for oid in (OWNER, OTHER):
            await pool.execute("DELETE FROM account_rehab_events WHERE owner_id=$1", oid)
            await pool.execute("DELETE FROM account_rehab_state WHERE owner_id=$1", oid)
            await pool.execute("DELETE FROM rehab_settings WHERE owner_id=$1", oid)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", oid)
    _run(_c())
    yield
    _run(_c())


def _acc(pool, owner=OWNER, status="spamblock"):
    return _run(pool.fetchval(
        "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
        "VALUES($1,$2,$3,TRUE,$4) RETURNING id",
        owner, f"+7999{os.urandom(3).hex()}", "s" * 40, status))


def test_relapse_after_release_is_picked_up_again(pool):
    from services import account_rehab as ar

    acc = _acc(pool)
    _run(ar._sync_enrollment(pool))
    assert _run(pool.fetchval("SELECT phase FROM account_rehab_state WHERE acc_id=$1", acc)) == "appeal"
    # вернули в строй
    _run(pool.execute("UPDATE tg_accounts SET acc_status='active' WHERE id=$1", acc))
    _run(ar._sync_enrollment(pool))
    assert _run(pool.fetchval("SELECT phase FROM account_rehab_state WHERE acc_id=$1", acc)) == "freed"
    # через месяц — снова блок
    _run(pool.execute("UPDATE tg_accounts SET acc_status='spamblock' WHERE id=$1", acc))
    _run(ar._sync_enrollment(pool))
    row = _run(pool.fetchrow(
        "SELECT phase, relapses, attempts FROM account_rehab_state WHERE acc_id=$1", acc))
    assert row["phase"] == "appeal", "повторный блок не подхвачен"
    assert row["relapses"] == 1 and row["attempts"] == 0
    steps = [r["step"] for r in _run(pool.fetch(
        "SELECT step FROM account_rehab_events WHERE acc_id=$1 ORDER BY id", acc))]
    assert steps == ["enrolled", "relapse"]
    due = _run(ar._due_rows(pool, 50))
    assert acc in [r["acc_id"] for r in due]


def test_paused_owner_is_not_processed(pool):
    from services import account_rehab as ar

    acc = _acc(pool)
    _run(ar._sync_enrollment(pool))
    _run(ar.save_settings(pool, OWNER, {"paused": True}))
    assert acc not in [r["acc_id"] for r in _run(ar._due_rows(pool, 500))]
    _run(ar.save_settings(pool, OWNER, {"paused": False}))
    assert acc in [r["acc_id"] for r in _run(ar._due_rows(pool, 500))]


def test_settings_roundtrip_and_isolation(pool):
    from services import account_rehab as ar

    s = _run(ar.save_settings(pool, OWNER, {"pace": "careful", "rest_hours": 48,
                                            "notify_freed": False}))
    assert s["pace"] == "careful"
    got = _run(ar.get_settings(pool, OWNER))
    assert got["rest_hours"] == 48 and got["notify_freed"] is False
    # частичное сохранение не сбрасывает остальное
    _run(ar.save_settings(pool, OWNER, {"paused": True}))
    got = _run(ar.get_settings(pool, OWNER))
    assert got["pace"] == "careful" and got["paused"] is True
    assert _run(ar.get_settings(pool, OTHER)) == ar.DEFAULT_SETTINGS


def test_advance_with_release_date_and_rest(pool):
    from datetime import datetime, timedelta, timezone

    from services import account_rehab as ar

    acc = _acc(pool)
    _run(ar._sync_enrollment(pool))
    rel = datetime.now(timezone.utc) + timedelta(days=5)
    _run(ar._advance(pool, acc, phase="warming", note="t", release_at=rel, delay_sec=60))
    got = _run(pool.fetchval("SELECT release_at FROM account_rehab_state WHERE acc_id=$1", acc))
    assert abs((got - rel).total_seconds()) < 1
    # без новой даты старая не затирается
    _run(ar._advance(pool, acc, phase="recheck", note="t2"))
    assert _run(pool.fetchval("SELECT release_at FROM account_rehab_state WHERE acc_id=$1", acc))
    _run(ar._rest_after_release(pool, acc, OWNER, 24))
    cd = _run(pool.fetchval("SELECT cooldown_until FROM tg_accounts WHERE id=$1", acc))
    assert cd > datetime.now(timezone.utc) + timedelta(hours=23)
    # короткий отдых не укорачивает уже стоящий длинный
    _run(ar._rest_after_release(pool, acc, OWNER, 1))
    assert _run(pool.fetchval("SELECT cooldown_until FROM tg_accounts WHERE id=$1", acc)) >= cd


def test_views_history_overview_and_bulk_restart(pool):
    from services import account_rehab as ar

    a1, a2, a3 = _acc(pool), _acc(pool), _acc(pool, status="frozen")
    _acc(pool, owner=OTHER)
    _run(ar._sync_enrollment(pool))
    _run(pool.execute(
        "INSERT INTO account_rehab_state(acc_id, owner_id, phase, note) VALUES($1,$2,'stuck',$3)",
        a3, OWNER, ar._FROZEN_NOTE))
    _run(pool.execute("UPDATE account_rehab_state SET phase='stuck' WHERE acc_id=$1", a2))
    assert {r["acc_id"] for r in _run(ar.list_for_owner(pool, OWNER, view="active"))} == {a1}
    assert {r["acc_id"] for r in _run(ar.list_for_owner(pool, OWNER, view="attention"))} == {a2, a3}
    assert len(_run(ar.list_for_owner(pool, OWNER))) == 3
    ov = _run(ar.rehab_overview(pool, OWNER))
    assert ov["in_progress"] == 1 and ov["stuck"] == 2 and ov["frozen"] == 1
    n = _run(ar.restart_all_stuck(pool, OWNER))
    assert n == 1, "перезапуск задел замороженный или не задел застрявший"
    assert _run(pool.fetchval("SELECT phase FROM account_rehab_state WHERE acc_id=$1", a3)) == "stuck"
    _run(ar.force_now(pool, OWNER, a1))
    hist = _run(ar.history(pool, OWNER, a1))
    assert [h["step"] for h in hist][:2] == ["manual_now", "enrolled"]
    assert hist[0]["label"].startswith("⏩")
    # чужая история не видна
    assert _run(ar.history(pool, OTHER, a1)) == []
