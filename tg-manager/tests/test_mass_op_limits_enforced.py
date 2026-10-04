"""Лимиты массовых операций обязаны ограничивать, а не только существовать.

ЧТО БЫЛО (жалоба владельца 04.10.2026: «лимиты есть, но не соблюдаются»):

  * Вступление и выход: дневной лимит аккаунта (`_JOIN_DAY_LIMITS`,
    `_LEAVE_DAY_LIMITS`) сверялся ОДИН раз — перед первой ссылкой аккаунта, —
    после чего аккаунт отрабатывал ВЕСЬ список. Лимит 20 при 50 ссылках давал
    50 вступлений одним аккаунтом за прогон.
  * Дневной лимит ЛС-кампании не видел разовых рассылок: аккаунт, разославший
    их утром, получал в кампании ещё полный лимит.
  * Исчерпали бюджет ВСЕ аккаунты — `select_all_active` возвращал весь флот
    «с предупреждением в лог», то есть лимит переставал действовать ровно там,
    где он нужнее всего.
  * Бюджет проверялся только на старте: аккаунт с остатком 1 проходил отбор и
    делал за прогон сколько угодно.
"""
from __future__ import annotations

import pytest


class _Pool:
    """Пул: журнал вступлений/выходов «сегодня» = today, остальное пусто."""

    def __init__(self, today: int = 0):
        self.today = today
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        if "operation_audit" in query:
            return self.today
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


async def _zero():
    return 0.0


def _quiet(monkeypatch, budget_left=None):
    import asyncio

    from services import op_worker

    async def _instant(*a, **kw):
        return None

    async def _not_quarantined(pool, acc_id):
        return False

    async def _budget(pool, ids):
        return dict(budget_left or {})

    monkeypatch.setattr(asyncio, "sleep", _instant)
    monkeypatch.setattr(op_worker, "_governed_delay", lambda pool, owner, base: _zero())
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    monkeypatch.setattr(op_worker, "_budget_remaining", _budget)


_ACC = {"id": 1, "phone": "+70000000001", "session_str": "s1"}


# ── вступление ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_join_day_limit_holds_inside_the_run(monkeypatch):
    from services import account_manager, op_worker

    attempted: list[str] = []

    async def _join(session_str, link, _acc=None):
        attempted.append(link)
        return {"ok": True}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    _quiet(monkeypatch)

    links = [f"t.me/c{i}" for i in range(30)]
    res = await op_worker._exec_bulk_join_inner(
        _Pool(today=0), object(), 77, 555,
        {"links": links, "delay_mode": "fast"}, [_ACC])

    assert len(attempted) == 20, (
        f"лимит вступлений «fast» = 20 в сутки, а аккаунт вступил {len(attempted)} раз"
    )
    assert "дневного лимита: 10" in res["summary"], res["summary"]


@pytest.mark.asyncio
async def test_join_counts_what_was_done_today(monkeypatch):
    from services import account_manager, op_worker

    attempted: list[str] = []

    async def _join(session_str, link, _acc=None):
        attempted.append(link)
        return {"ok": True}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    _quiet(monkeypatch)

    await op_worker._exec_bulk_join_inner(
        _Pool(today=17), object(), 77, 555,
        {"links": [f"t.me/c{i}" for i in range(10)], "delay_mode": "fast"}, [_ACC])
    assert len(attempted) == 3, "17 вступлений сегодня + лимит 20 = ещё 3"


@pytest.mark.asyncio
async def test_join_respects_daily_action_budget(monkeypatch):
    from services import account_manager, op_worker

    attempted: list[str] = []

    async def _join(session_str, link, _acc=None):
        attempted.append(link)
        return {"ok": True}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    _quiet(monkeypatch, budget_left={1: 4})

    await op_worker._exec_bulk_join_inner(
        _Pool(today=0), object(), 77, 555,
        {"links": [f"t.me/c{i}" for i in range(10)], "delay_mode": "fast"}, [_ACC])
    assert len(attempted) == 4, "остаток суточного бюджета 4 — больше 4 действий нельзя"


# ── выход ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leave_day_limit_holds_inside_the_run(monkeypatch):
    from services import account_manager, op_worker

    attempted: list[str] = []

    async def _leave(session_str, channel, _acc=None):
        attempted.append(channel)
        return {"ok": True}

    async def _resolve(pool, owner_id, ids):
        return [dict(_ACC)]

    async def _claim(op_id, rows, owner_id):
        return rows

    async def _release(ids):
        return None

    monkeypatch.setattr(account_manager, "leave_channel", _leave)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active",
                        lambda *a, **k: _resolve(None, None, None))
    monkeypatch.setattr(op_worker, "_claim_available_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    _quiet(monkeypatch)

    res = await op_worker._exec_bulk_leave(
        _Pool(today=0), object(), 77, 555,
        {"channels": [f"t.me/c{i}" for i in range(40)], "delay_mode": "fast",
         "account_ids": [1]})
    assert len(attempted) == 25, (
        f"лимит выходов «fast» = 25 в сутки, а аккаунт вышел {len(attempted)} раз"
    )
    assert res["ok"] == 25


# ── общий суточный бюджет ────────────────────────────────────────────────────

def test_budget_does_not_cut_invites_and_dms():
    """Инвайты и ЛС — основной объём продукта, у них свои умные суточные лимиты.
    Общий бюджет в 50 действий их не режет, иначе день инвайтов закрывал бы
    аккаунту публикации и вступления."""
    from services import account_budget as ab

    assert "dm" not in ab._COUNTED_ACTIONS and "invite" not in ab._COUNTED_ACTIONS
    assert "account_daily_stats" not in ab._COUNT_SQL


@pytest.mark.asyncio
async def test_record_actions_writes_one_row_per_action():
    from services import account_budget as ab

    pool = _Pool()
    await ab.record_actions(pool, 555, 1, "dm", 3, result="sent", operation_id=9)
    assert len(pool.executed) == 1
    q, args = pool.executed[0]
    assert "operation_audit" in q and "generate_series" in q
    assert args == (555, 9, 1, "dm", "sent", 3)

    # Действие вне учёта писать бессмысленно — его никто не прочтёт.
    await ab.record_actions(pool, 555, 1, "health_check", 1)
    assert len(pool.executed) == 1


@pytest.mark.asyncio
async def test_remaining_budget_per_account(monkeypatch):
    from services import account_budget as ab

    class _P(_Pool):
        async def fetch(self, query, *args):
            return [{"account_id": 1, "n": 48}, {"account_id": 2, "n": 70}]

    left = await ab.remaining_bulk(_P(), [1, 2, 3], limit=50)
    assert left == {1: 2, 2: 0, 3: 50}
    assert await ab.remaining_bulk(_P(), [1], limit=0) == {1: None}


@pytest.mark.asyncio
async def test_selector_returns_nobody_when_whole_fleet_is_over_budget(monkeypatch):
    from services import account_budget, resource_selector

    row = {"id": 1, "proxy_url": None, "proxy_alive": None, "proxy_fail_streak": 0}

    class _P(_Pool):
        async def fetch(self, query, *args):
            return [row]

    async def _all_over(pool, ids, limit=None):
        return [], list(ids)

    monkeypatch.setattr(account_budget, "filter_within_budget", _all_over)
    rows = await resource_selector.select_all_active(
        _P(), 555, respect_daily_budget=True)
    assert rows == [], "все исчерпали суточный бюджет — работать ими нельзя"
