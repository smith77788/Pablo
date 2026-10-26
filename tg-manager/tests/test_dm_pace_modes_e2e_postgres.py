"""Все режимы темпа DM-кампании по НАСТОЯЩЕМУ Postgres после правки учёта
доставки (send_dm со strict=True, исход 'unconfirmed').

Заглушена только граница с Telegram — `account_manager.send_message`, то есть
настоящий `send_dm` с его классификацией ошибок исполняется целиком. Для
каждого темпа (slow/normal/fast/max/auto и темп по умолчанию) проверяем:
кампания доходит до конца, паузы между сообщениями соответствуют множителю
темпа, а в журнале «отправлено» стоит ровно у тех, кого Telegram принял.

Без `INFRAGRAM_TEST_DSN` файл пропускается (стенд — см. test_mass_ops_e2e_postgres).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990778
CONTACTS = 6
PEER_FLOOD_USER = "u2"   # аккаунт под спам-блоком: PeerFlood
TIMEOUT_USER = "u4"      # запрос ушёл, ответа нет
# crm-аудитория: базовая пауза 45–110 с (dm_engine._DELAYS_BY_TARGET_TYPE).
BASE_MIN, BASE_MAX = 45.0, 110.0

_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class PeerFloodError(Exception):
    pass


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    await conn.execute(fh.read())
            except Exception:
                pass
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture
def telegram(monkeypatch):
    """Граница с Telegram + запись пауз кампании."""
    from services import account_manager, dm_engine

    calls: list = []
    sleeps: list[float] = []

    async def _send_message(session, dest, text, _acc=None, strict=False):
        calls.append((dest, strict))
        if dest == PEER_FLOOD_USER:
            raise PeerFloodError("PEER_FLOOD")
        if dest == TIMEOUT_USER:
            raise TimeoutError()
        return True

    async def _sleep(op_id, seconds, pool=None):
        sleeps.append(float(seconds))

    monkeypatch.setattr(account_manager, "send_message", _send_message)
    monkeypatch.setattr(dm_engine, "_guarded_sleep", _sleep)
    return calls, sleeps


async def _seed(p, pace):
    await p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    for i in range(2):
        await p.execute(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
            "acc_status,first_name) VALUES($1,$2,$3,TRUE,'active',$4)",
            OWNER, f"+7996{i:07d}", f"sess{i}", f"Acc{i}")
    for i in range(CONTACTS):
        await p.execute(
            "INSERT INTO crm_contacts(owner_id,tg_user_id,username) VALUES($1,$2,$3)",
            OWNER, 610000 + i, f"u{i}")
    params = {"quiet_hours": False}
    if pace is not None:
        params["pace"] = pace
    return await p.fetchval(
        "INSERT INTO dm_campaigns(owner_id,name,text_template,target_type,status,params) "
        "VALUES($1,'pace','Привет','crm','pending',$2::jsonb) RETURNING id",
        OWNER, json.dumps(params))


@pytest.mark.parametrize("pace", ["slow", "normal", "fast", "max", "auto", None])
def test_every_pace_runs_and_counts_only_delivered(pool, telegram, pace):
    from services import dm_engine

    calls, sleeps = telegram
    cid = _run(_seed(pool, pace))
    _run(dm_engine.run_campaign(pool, None, cid))

    # Отправка шла строго: ошибки доходят до классификатора кампании.
    assert calls and all(strict for _, strict in calls)

    camp = _run(pool.fetchrow(
        "SELECT status, sent_count, fail_count FROM dm_campaigns WHERE id=$1", cid))
    log = dict(_run(pool.fetch(
        "SELECT status, COUNT(*) AS n FROM dm_campaign_log WHERE campaign_id=$1 "
        "GROUP BY status", cid)))
    delivered = CONTACTS - 2
    assert camp["status"] == "done", camp
    assert camp["sent_count"] == delivered
    assert log.get("sent") == delivered, log
    assert log.get("peer_flood") == 1, log
    assert log.get("unconfirmed") == 1, log

    # Отчёт по аккаунтам (тот же SQL, что у экрана «Отчёт кампании»).
    by_acc = _run(pool.fetchval(
        "SELECT COUNT(*) FILTER (WHERE status='sent') FROM dm_campaign_log "
        "WHERE campaign_id=$1", cid))
    assert by_acc == delivered

    # Паузы между сообщениями: по одной на обработанную цель, в рамках
    # множителя выбранного темпа (губернатор флота может только растянуть).
    pacing = [s for s in sleeps if s not in (1.0, 60)]
    assert len(pacing) >= delivered
    if pace == "auto":
        assert all(s > 0 for s in pacing)
    else:
        mult = dm_engine._PACE_MULTIPLIERS.get(pace or "normal", 1.0)
        assert min(pacing) >= BASE_MIN * mult * 0.999, (pace, pacing)
        assert max(pacing) <= BASE_MAX * mult * 10, (pace, pacing)
