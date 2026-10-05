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


# ── дверь бота: доля остановившегося аккаунта не пропадает ────────────────────

class _Msg:
    def __init__(self):
        self.texts: list[str] = []
        self.bot = None

    async def answer(self, text, **kw):
        self.texts.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.texts.append(text)


class _DoorPool:
    async def fetchval(self, *a):
        return None


@pytest.mark.asyncio
async def test_bot_door_hands_stopped_accounts_share_to_others(monkeypatch):
    from bot.handlers import channel_ops
    from services import resource_selector, task_registry

    accounts = [{"id": 1, "first_name": "A", "phone": "", "session_str": "s1"},
                {"id": 2, "first_name": "B", "phone": "", "session_str": "s2"}]
    calls: list = []

    async def _inv(sess, ch, refs, _acc=None, **kw):
        calls.append((_acc["id"], list(refs)))
        if _acc["id"] == 2:
            # второй аккаунт сразу ловит длинную паузу — никого не пригласил
            return {"invited": 0, "invited_list": [], "failed": [], "untried": list(refs),
                    "error": "Пауза Telegram", "flood_wait": 900}
        return {"invited": len(refs), "invited_list": list(refs), "failed": [],
                "untried": []}

    async def _filter(pool, owner, keys, refs):
        return list(refs), 0, 0

    tasks: list = []
    monkeypatch.setattr(am, "invite_users_to_channel", _inv)
    monkeypatch.setattr(am, "join_channel_by_id", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(am, "promote_to_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(resource_selector, "select_all_active", AsyncMock(return_value=accounts))
    monkeypatch.setattr(idd, "filter_new", _filter)
    monkeypatch.setattr(idd, "account_allowance", AsyncMock(return_value=(100, "")))
    monkeypatch.setattr(idd, "settle", AsyncMock())
    monkeypatch.setattr(task_registry, "register", lambda u, k, l, t: tasks.append(t))
    monkeypatch.setattr(channel_ops.asyncio, "sleep", AsyncMock())

    msg = _Msg()
    trigger = SimpleNamespace(from_user=SimpleNamespace(id=5), answer=msg.answer, bot=None)
    trigger.edit_text = msg.edit_text
    refs = [f"@u{i}" for i in range(30)]
    await channel_ops._run_invite_bg(
        refs, trigger, _DoorPool(),
        {"channel_id": 100, "inv_selected_accounts": [1, 2], "primary_acc_id": 1})
    await asyncio.gather(*tasks)

    invited_by_1 = [r for aid, chunk in calls if aid == 1 for r in chunk]
    assert sorted(invited_by_1) == sorted(refs), "доля зафлуженного должна уйти живому"
    assert "Не дошла очередь" not in msg.texts[-1]


def test_bot_doors_pass_pool_and_limit():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "bot" / "handlers"
           / "channel_ops.py").read_text(encoding="utf-8")
    calls = src.count("_am.invite_users_to_channel(")
    assert src.count("_idd.account_allowance(") >= calls
    assert src.count("max_invites=") >= calls
    assert src.count("pool=pool,") >= calls


@pytest.mark.asyncio
async def test_bot_door_records_join_flood_and_promotes_with_pool(monkeypatch):
    """Пауза на вступлении и выдаче прав ложится в пульс в базе."""
    from bot.handlers import channel_ops
    from services import resource_selector, task_registry

    accounts = [{"id": 1, "first_name": "A", "phone": "", "session_str": "s1"},
                {"id": 2, "first_name": "B", "phone": "", "session_str": "s2",
                 "tg_user_id": 0},
                {"id": 3, "first_name": "C", "phone": "", "session_str": "s3",
                 "tg_user_id": 33}]
    floods: list = []
    promote_kw: list = []

    async def _join(sess, ch, ah, _acc=None):
        if _acc["id"] == 2:
            return {"ok": False, "error": "пауза", "flood_wait": 500}
        return {"ok": True}

    async def _promote(*a, **k):
        promote_kw.append(k)
        return True

    async def _rec(pool, acc, secs, action="default", operation_id=None):
        floods.append((pool, acc, secs, action))
        return secs

    async def _inv(sess, ch, refs, _acc=None, **kw):
        return {"invited": len(refs), "invited_list": list(refs), "failed": [], "untried": []}

    async def _filter(pool, owner, keys, refs):
        return list(refs), 0, 0

    tasks: list = []
    pool = _DoorPool()
    monkeypatch.setattr(am, "invite_users_to_channel", _inv)
    monkeypatch.setattr(am, "join_channel_by_id", _join)
    monkeypatch.setattr(am, "promote_to_admin", _promote)
    monkeypatch.setattr(flood_engine, "record_flood", _rec)
    monkeypatch.setattr(resource_selector, "select_all_active", AsyncMock(return_value=accounts))
    monkeypatch.setattr(idd, "filter_new", _filter)
    monkeypatch.setattr(idd, "account_allowance", AsyncMock(return_value=(100, "")))
    monkeypatch.setattr(idd, "settle", AsyncMock())
    monkeypatch.setattr(task_registry, "register", lambda u, k, l, t: tasks.append(t))
    monkeypatch.setattr(channel_ops.asyncio, "sleep", AsyncMock())

    msg = _Msg()
    trigger = SimpleNamespace(from_user=SimpleNamespace(id=5), answer=msg.answer, bot=None)
    await channel_ops._run_invite_bg(
        ["@a", "@b"], trigger, pool,
        {"channel_id": 100, "inv_selected_accounts": [1, 2, 3], "primary_acc_id": 1})
    await asyncio.gather(*tasks)
    assert (pool, 2, 500, "join") in floods
    assert promote_kw and all(k.get("pool") is pool for k in promote_kw)


def test_contacts_door_records_join_flood_too():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "bot" / "handlers"
           / "channel_ops.py").read_text(encoding="utf-8")
    assert src.count("await _note_join_flood(") >= 2
    import re
    for m in re.finditer(r"_am\.promote_to_admin\((.*?)\n\s*\)", src, re.S):
        assert "pool=pool" in m.group(1), "выдача прав в двери бота без пула"
