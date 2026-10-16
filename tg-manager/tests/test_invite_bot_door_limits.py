"""Инвайт из бота живёт по тем же правилам, что массовый инвайт.

Дыры, которые были закрыты:
  • пауза Telegram писалась только в память процесса (pool=None): в базе
    аккаунт оставался «спокойным», и подбор под следующую операцию уводил его
    под действующее ограничение;
  • длинная пауза пережидалась не больше 10 минут и сразу пробовалась снова —
    повтор падал, цель списывалась в ошибки;
  • PeerFlood не записывался вовсе, а цель, на которой он пришёл, пропадала
    из отчёта;
  • умный суточный лимит и карантин бот не смотрел, а приглашённых не вносил в
    суточный учёт — массовый инвайт потом считал аккаунт свежим;
  • доля остановившегося аккаунта пропадала: список делился поровну заранее.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from telethon.errors import (  # стаб-классы из conftest
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
)
from services import account_manager as am
from services import flood_engine
from services import invite_dedup as idd


def _flood(seconds: int) -> FloodWaitError:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


class _Client:
    def __init__(self, effects):
        self.effects = list(effects)
        self.sent: list = []
        self.disconnect = AsyncMock()

    async def connect(self):
        return None

    def is_connected(self):
        return True

    async def get_entity(self, ref):
        return ref

    async def __call__(self, req):
        eff = self.effects.pop(0) if self.effects else None
        if isinstance(eff, Exception):
            raise eff
        self.sent.append(req)
        return eff


async def _invite(effects, refs, **kw):
    client = _Client(effects)
    with patch.object(am, "_make_client", return_value=client), \
         patch.object(am, "timeboxed", lambda c: c), \
         patch.object(am, "_resolve_channel_peer", AsyncMock(return_value=object())), \
         patch.object(am.asyncio, "sleep", AsyncMock()):
        return await am.invite_users_to_channel("s", 100, refs, _acc={"id": 7}, **kw)


POOL = object()


@pytest.mark.asyncio
async def test_long_flood_is_recorded_in_db_and_rest_returned():
    seen = []

    async def _rec(pool, acc, secs, action="default", operation_id=None):
        seen.append((pool, acc, secs))
        return secs
    with patch.object(flood_engine, "record_flood", _rec):
        res = await _invite([None, _flood(900)], ["@a", "@b", "@c", "@d"], pool=POOL)
    assert seen == [(POOL, 7, 900)], "пауза должна лечь в базу, а не только в память"
    assert res["invited_list"] == ["@a"]
    assert res["untried"] == ["@b", "@c", "@d"]
    assert res["failed"] == [], "флуд — отказ аккаунту, не людям"
    assert res["flood_wait"] == 900


@pytest.mark.asyncio
async def test_short_flood_retries_same_target_once():
    with patch.object(flood_engine, "record_flood", AsyncMock(return_value=0)):
        res = await _invite([_flood(10), None, None], ["@a", "@b"], pool=POOL)
    assert res["invited_list"] == ["@a", "@b"]
    assert not res["untried"] and not res["failed"]


@pytest.mark.asyncio
async def test_peer_flood_recorded_and_current_target_kept():
    rec = AsyncMock(return_value=0)
    with patch.object(flood_engine, "record_peer_flood", rec):
        res = await _invite([None, PeerFloodError("x")], ["@a", "@b", "@c"], pool=POOL)
    rec.assert_awaited_once_with(POOL, 7, "invite")
    assert res["untried"] == ["@b", "@c"]
    assert res["failed"] == []
    assert res.get("peer_flood")


@pytest.mark.asyncio
async def test_no_rights_returns_everyone_untried():
    res = await _invite([ChatAdminRequiredError("x")], ["@a", "@b"])
    assert res["untried"] == ["@a", "@b"] and res["failed"] == []


@pytest.mark.asyncio
async def test_daily_limit_stops_the_account():
    res = await _invite([], ["@a", "@b", "@c", "@d", "@e"], max_invites=2)
    assert res["invited_list"] == ["@a", "@b"]
    assert res["untried"] == ["@c", "@d", "@e"]
    assert res.get("limit_reached")


@pytest.mark.asyncio
async def test_exhausted_limit_does_not_even_connect():
    with patch.object(am, "_make_client") as mk:
        mk.return_value = _Client([])
        res = await am.invite_users_to_channel("s", 1, ["@a"], _acc={"id": 7},
                                               max_invites=0)
    assert res["untried"] == ["@a"] and res["invited"] == 0


# ── лимит и учёт для дверей ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_allowance_respects_quarantine_and_daily_limit(monkeypatch):
    from services import infra_memory

    monkeypatch.setattr(flood_engine, "is_account_cooling", lambda a: False)
    monkeypatch.setattr(infra_memory, "is_account_quarantined", AsyncMock(return_value=True))
    left, why = await idd.account_allowance(POOL, 7)
    assert left == 0 and "карантин" in why

    monkeypatch.setattr(infra_memory, "is_account_quarantined", AsyncMock(return_value=False))
    monkeypatch.setattr(flood_engine, "recommended_daily_limit", AsyncMock(
        return_value={"limit": 30, "used_today": 30, "remaining": 0}))
    left, why = await idd.account_allowance(POOL, 7)
    assert left == 0 and "лимит" in why

    monkeypatch.setattr(flood_engine, "recommended_daily_limit", AsyncMock(
        return_value={"limit": 30, "used_today": 12, "remaining": 18}))
    assert await idd.account_allowance(POOL, 7) == (18, "")


@pytest.mark.asyncio
async def test_settle_counts_bot_invites_in_daily_stats(monkeypatch):
    from services import op_worker

    bump = AsyncMock()
    monkeypatch.setattr(op_worker, "bump_daily_stats", bump)
    monkeypatch.setattr(idd, "remember", AsyncMock(return_value=True))
    await idd.settle(POOL, 1, ["k"], 7, {"invited": 3, "invited_list": ["a", "b", "c"],
                                         "failed": ["x: privacy"], "flood_wait": 300})
    bump.assert_awaited_once_with(POOL, 7, ok=3, fail=1, invites=3, floods=1)


# ── дверь карточки канала: та же операция, что у инвайтера и мини-аппа ─────────

class _Msg:
    def __init__(self):
        self.texts: list[str] = []
        self.bot = None

    async def answer(self, text, **kw):
        self.texts.append(text)
        return self


class _DoorPool:
    def __init__(self, username=None):
        self.username = username

    async def fetchval(self, *a):
        return self.username


async def _door(monkeypatch, refs, fsm, username=None, submit=None, fresh=None):
    from bot.handlers import channel_ops
    from services import operation_bus, task_registry

    seen: list = []

    async def _submit(pool, owner, op_type, params, **kw):
        seen.append((owner, op_type, params, kw))
        return 42

    async def _filter(pool, owner, keys, refs_):
        keep = [r for r in refs_ if fresh is None or r in fresh]
        return keep, len(refs_) - len(keep), 0

    tasks: list = []
    monkeypatch.setattr(operation_bus, "submit", submit or _submit)
    monkeypatch.setattr(idd, "filter_new", _filter)
    monkeypatch.setattr(task_registry, "register", lambda *a: tasks.append(a))
    msg = _Msg()
    trigger = SimpleNamespace(from_user=SimpleNamespace(id=5), answer=msg.answer, bot=None)
    await channel_ops._run_invite_bg(refs, trigger, _DoorPool(username), fsm)
    return seen, msg, tasks


@pytest.mark.asyncio
async def test_card_door_queues_mass_invite_instead_of_inprocess_task(monkeypatch):
    """Задача в памяти процесса обрывалась деплоем и шла мимо тарифа и Ban Weather."""
    seen, msg, tasks = await _door(
        monkeypatch, ["@a", "@b", "@c"],
        {"channel_id": 100, "channel_display": "Мой канал",
         "inv_selected_accounts": [3, 1, 2], "primary_acc_id": 1},
        username="mychan", fresh={"@a", "@c"})
    assert not tasks, "инвайт не должен жить задачей в памяти процесса"
    assert len(seen) == 1
    owner, op_type, params, kw = seen[0]
    assert (owner, op_type) == (5, "mass_invite")
    assert params["group"] == "@mychan"
    assert params["user_refs"] == ["@a", "@c"], "уже приглашённых в очередь не ставим"
    assert params["account_ids"][0] == 1, "владелец канала — первым: он выдаёт права"
    assert sorted(params["account_ids"]) == [1, 2, 3]
    assert kw["total_items"] == 2
    assert "#42" in msg.texts[-1] and "пропущено: 1" in msg.texts[-1]


@pytest.mark.asyncio
async def test_card_door_private_channel_goes_by_id(monkeypatch):
    seen, _, _ = await _door(
        monkeypatch, ["@a"],
        {"channel_id": 2959208816, "inv_selected_accounts": [1], "primary_acc_id": 1})
    assert seen[0][2]["group"] == "2959208816"
    from services.mass_inviter_engine import _as_channel_id
    assert _as_channel_id(seen[0][2]["group"]) == 2959208816


@pytest.mark.asyncio
async def test_card_door_respects_plan_gate(monkeypatch):
    from services.operation_bus import PlanRequiredError

    async def _locked(*a, **k):
        raise PlanRequiredError("mass_invite", "pro")
    seen, msg, _ = await _door(
        monkeypatch, ["@a"],
        {"channel_id": 100, "inv_selected_accounts": [1], "primary_acc_id": 1},
        submit=_locked)
    assert "#" not in msg.texts[-1] and "Инвайт в канал" in msg.texts[-1]


@pytest.mark.asyncio
async def test_card_door_nothing_new_queues_nothing(monkeypatch):
    seen, msg, _ = await _door(
        monkeypatch, ["@a"],
        {"channel_id": 100, "inv_selected_accounts": [1], "primary_acc_id": 1},
        fresh=set())
    assert not seen and "Новых для этого канала нет" in msg.texts[-1]


def test_bot_doors_pass_pool_and_limit():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "bot" / "handlers"
           / "channel_ops.py").read_text(encoding="utf-8")
    calls = src.count("_am.invite_users_to_channel(")
    assert src.count("_idd.account_allowance(") >= calls
    assert src.count("max_invites=") >= calls
    assert src.count("pool=pool,") >= calls


def test_contacts_door_records_join_flood_too():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "bot" / "handlers"
           / "channel_ops.py").read_text(encoding="utf-8")
    assert src.count("await _note_join_flood(") >= 1
    import re
    for m in re.finditer(r"_am\.promote_to_admin\((.*?)\n\s*\)", src, re.S):
        assert "pool=pool" in m.group(1), "выдача прав в двери бота без пула"
