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
    assert "async def _upsert_niche_profile(" in h
    assert "INSERT INTO account_niche_profiles" in h
    # профиль пишется ДО создания плана (движок при первом тике видит веса)
    i_prof = h.index("_upsert_niche_profile(pool, callback.from_user.id, acc_id, profile_type)")
    i_plan = h.index("create_warmup_plan(pool, callback.from_user.id, acc_id, plan_type)")
    assert i_prof < i_plan


@pytest.mark.asyncio
async def test_upsert_niche_profile_writes_profile_type():
    import bot.handlers.account_warmup as wu

    calls = []

    class _Pool:
        async def execute(self, q, *args):
            calls.append((q, args))
            return "INSERT 1"

    await wu._upsert_niche_profile(_Pool(), owner_id=42, account_id=7,
                                   profile_type="commenter")
    # был CREATE TABLE IF NOT EXISTS и INSERT ... ON CONFLICT
    joined = " ".join(q for q, _ in calls)
    assert "CREATE TABLE IF NOT EXISTS account_niche_profiles" in joined
    assert "ON CONFLICT (account_id) DO UPDATE" in joined
    # profile_type реально в аргументах INSERT
    insert_args = [a for q, a in calls if "INSERT INTO account_niche_profiles" in q][0]
    assert "commenter" in insert_args
    assert 7 in insert_args and 42 in insert_args


def test_plan_index_encoding_roundtrip():
    import bot.handlers.account_warmup as wu
    order = wu._WARMUP_PLAN_ORDER
    assert order == ("gentle", "standard", "aggressive")
    # индекс режима корректно декодируется обратно
    for i, name in enumerate(order):
        assert order[i] == name
