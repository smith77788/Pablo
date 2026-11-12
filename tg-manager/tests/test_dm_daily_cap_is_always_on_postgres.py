"""Суточный потолок холодных ЛС с аккаунта есть ВСЕГДА, а дойдя до него,
рассылка не умирает, а продолжается на следующие сутки сама.

НАХОДКА. `per_account_daily` у кампании и `per_account_cap` у разовой
рассылки были необязательными: не заданный лимит означал «без лимита». Кампания
на тысячу человек с двумя аккаунтами слала с каждого по пятьсот холодных ЛС за
день — Telegram ставит PeerFlood задолго до этого, то есть «без лимита»
значило «до флага на аккаунте».

ВТОРАЯ НАХОДКА. Когда лимит всё-таки был задан и его выбирали все аккаунты,
кампания вставала на паузу, «до следующего дня» — так было написано в
комментарии. Но продолжить её не мог никто, кроме владельца кнопкой. Разовая
рассылка делала хуже: остаток аудитории списывала строкой «пропущено» в журнал,
и эти люди не получали сообщения уже никогда (повтор считал их закрытыми).

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. effective_daily_cap → вернуть `None` для пустого -> красные
     test_unset_cap_is_a_safe_default и
     test_campaign_without_a_cap_stops_at_the_default_postgres;
  2. убрать `return CAMPAIGN_DAILY_CAP` (голый return) -> красные
     test_campaign_without_a_cap_stops_at_the_default_postgres и
     test_capped_campaign_is_deferred_to_tomorrow_postgres;
  3. не префиллить счёт за сегодня -> красный
     test_what_was_sent_today_by_other_paths_counts_postgres.
"""
from __future__ import annotations

import json

import pytest

from services import dm_engine
from tests.test_dm_pace_modes_e2e_postgres import (  # noqa: F401 — стенд
    OWNER, _run, pool,
)


@pytest.mark.parametrize("raw", [None, "", 0, "0", -5, "abc"])
def test_unset_cap_is_a_safe_default(raw):
    assert dm_engine.effective_daily_cap(raw) == dm_engine.DM_DAILY_CAP_DEFAULT


def test_cap_is_clamped_from_above():
    assert dm_engine.effective_daily_cap(10) == 10
    assert dm_engine.effective_daily_cap("150") == 150
    assert dm_engine.effective_daily_cap(5000) == dm_engine.DM_DAILY_CAP_MAX


def test_default_is_conservative_for_cold_dms():
    # Холодные ЛС с обычного аккаунта: десятки в сутки, не сотни.
    assert 10 <= dm_engine.DM_DAILY_CAP_DEFAULT <= 50
    assert dm_engine.DM_DAILY_CAP_MAX <= 200


N_TARGETS = dm_engine.DM_DAILY_CAP_DEFAULT + 7


@pytest.fixture
def telegram(monkeypatch):
    from services import account_manager

    sent: list = []

    async def _send_message(session, dest, text, _acc=None, strict=False):
        sent.append((dest, (_acc or {}).get("id")))
        return True

    async def _sleep(op_id, seconds, pool=None):
        return None

    monkeypatch.setattr(account_manager, "send_message", _send_message)
    monkeypatch.setattr(dm_engine, "_guarded_sleep", _sleep)
    return sent


async def _seed(p, params: dict | None = None) -> tuple[int, int]:
    await p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM operation_audit WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    acc_id = await p.fetchval(
        "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
        "acc_status,first_name) VALUES($1,'+79960000099','sess-cap',TRUE,'active','Cap')"
        " RETURNING id", OWNER)
    for i in range(N_TARGETS):
        await p.execute(
            "INSERT INTO unified_contacts(owner_id,telegram_user_id,username,custom_fields)"
            " VALUES($1,$2,$3,jsonb_build_object('legacy_crm_id',$4::int))",
            OWNER, 620000 + i, f"cap{i}", i + 1)
    prm = {"quiet_hours": False, "pace": "max"}
    prm.update(params or {})
    cid = await p.fetchval(
        "INSERT INTO dm_campaigns(owner_id,name,text_template,target_type,status,params) "
        "VALUES($1,'cap','Привет','crm','pending',$2::jsonb) RETURNING id",
        OWNER, json.dumps(prm))
    return cid, acc_id


def test_campaign_without_a_cap_stops_at_the_default_postgres(pool, telegram):
    cid, _acc = _run(_seed(pool))
    stopped_by = _run(dm_engine.run_campaign(pool, None, cid))
    assert len(telegram) == dm_engine.DM_DAILY_CAP_DEFAULT, (
        f"кампания без заданного лимита отправила {len(telegram)} ЛС с одного "
        "аккаунта за день — «без лимита» снова значит «до PeerFlood»")
    assert stopped_by == dm_engine.CAMPAIGN_DAILY_CAP
    st = _run(pool.fetchval("SELECT status FROM dm_campaigns WHERE id=$1", cid))
    assert st == "paused"


def test_what_was_sent_today_by_other_paths_counts_postgres(pool, telegram):
    cid, acc = _run(_seed(pool, {"per_account_daily": 12}))
    # Утром тот же аккаунт разослал 10 ЛС разовой рассылкой.
    for _ in range(10):
        _run(pool.execute(
            "INSERT INTO operation_audit(owner_id, account_id, action, result) "
            "VALUES($1,$2,'dm','sent')", OWNER, acc))
    _run(dm_engine.run_campaign(pool, None, cid))
    assert len(telegram) == 2, (
        f"лимит 12, утром уже 10 — а кампания отправила {len(telegram)}")


def test_capped_campaign_is_deferred_to_tomorrow_postgres(pool, telegram, monkeypatch):
    """Исполнитель не завершает кампанию, а откладывает ту же операцию."""
    from services import op_worker

    async def _not_cancelled(*a, **k):
        return False
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    cid, _acc = _run(_seed(pool, {"per_account_daily": 5}))
    res = _run(op_worker._exec_dm_campaign(pool, None, 0, OWNER, {"campaign_id": cid}))
    assert len(telegram) == 5
    assert res["status"] == "requeue", res
    assert res["defer_cause"] == dm_engine.CAMPAIGN_DAILY_CAP
    # До начала следующих суток (по часам базы) плюс разброс, не больше суток+.
    left = float(_run(pool.fetchval(
        "SELECT EXTRACT(EPOCH FROM (date_trunc('day', now()) + interval '1 day' - now()))")))
    assert left + 20 * 60 - 5 <= res["defer_s"] <= left + 90 * 60 + 5, res["defer_s"]
    assert "завтра" in res["summary"]


def test_unreadable_count_holds_the_campaign(monkeypatch, pool, telegram):
    cid, _acc = _run(_seed(pool))

    async def _broken(_pool, _owner):
        raise dm_engine.DailyCountUnreadable("x")
    monkeypatch.setattr(dm_engine, "dm_sent_today", _broken)
    stopped_by = _run(dm_engine.run_campaign(pool, None, cid))
    assert stopped_by == dm_engine.CAMPAIGN_LIMIT_UNREADABLE
    assert telegram == [], "без счёта за сегодня ЛС уходить не должны"
