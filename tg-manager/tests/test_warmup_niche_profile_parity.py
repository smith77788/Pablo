"""Паритет: бот-прогрев задаёт поведенческий профиль (account_niche_profiles).

Раньше bot warmup всегда шёл mixed/general — profile_type/niche писались только
из mini-app, а движок account_warmer читает их из account_niche_profiles. Теперь
бот показывает выбор профиля и пишет ту же таблицу тем же upsert'ом.
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_engine_reads_niche_profile_table():
    # движок реально читает profile_type/niche из этой таблицы
    aw = _read("services/account_warmer.py")
    assert "FROM account_niche_profiles" in aw


def test_bot_offers_profile_choice_and_writes_table():
    h = _read("bot/handlers/account_warmup.py")
    for prof in ("prof_reader", "prof_commenter", "prof_reactor", "prof_lurker", "prof_mixed"):
        assert prof in h, f"нет профиля {prof}"
    # Профиль пишет общая дверь создания плана — в той же транзакции, что и
    # план (движок при первом тике видит веса, отказ не оставляет профиль
    # без плана). Своя запись профиля в боте снята.
    assert "profile_type=profile_type" in h
    assert "async def _upsert_niche_profile(" not in h
    aw = _read("services/account_warmer.py")
    assert "INSERT INTO account_niche_profiles" in aw


class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_create_plan_writes_profile_before_plan(monkeypatch):
    from services import account_warmer as aw
    from services import operation_bus

    async def _gates(*a, **kw):
        return None

    monkeypatch.setattr(operation_bus, "enforce_gates", _gates)
    calls = []

    class _Conn:
        async def execute(self, q, *args):
            calls.append((q, args))
            return "INSERT 1"

        async def fetchrow(self, q, *args):
            calls.append((q, args))
            return {"id": 11}

        def transaction(self):
            return _Ctx(None)

    class _Pool:
        async def fetchrow(self, q, *args):
            return {"id": 7, "session_str": "s", "acc_status": "active",
                    "is_active": True}

        async def execute(self, q, *args):
            return "UPDATE 1"

        def acquire(self):
            return _Ctx(_Conn())

    plan_id = await aw.create_warmup_plan(_Pool(), 42, 7, "gentle",
                                          profile_type="commenter")
    assert plan_id == 11
    qs = [q for q, _ in calls]
    i_prof = next(i for i, q in enumerate(qs) if "INSERT INTO account_niche_profiles" in q)
    i_plan = next(i for i, q in enumerate(qs) if "INSERT INTO account_warmup_plans" in q)
    assert i_prof < i_plan
    prof_args = calls[i_prof][1]
    assert "commenter" in prof_args and 7 in prof_args and 42 in prof_args


def test_plan_index_encoding_roundtrip():
    import bot.handlers.account_warmup as wu
    order = wu._WARMUP_PLAN_ORDER
    assert order == ("gentle", "standard", "aggressive")
    # индекс режима корректно декодируется обратно
    for i, name in enumerate(order):
        assert order[i] == name
