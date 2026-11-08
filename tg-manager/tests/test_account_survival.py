"""Выживаемость флота: доля потерь, тренд, причины, возраст и прокси."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from services import account_survival as sv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _ev(acc, new, days_ago, old="active", age_days=60, has_proxy=True):
    ts = NOW - timedelta(days=days_ago)
    return {"acc_id": acc, "old_status": old, "new_status": new, "created_at": ts,
            "added_at": (ts - timedelta(days=age_days)) if age_days is not None else None,
            "has_proxy": has_proxy}


def test_empty_fleet_is_empty_verdict():
    r = sv.summarize([], 0, now=NOW)
    assert r["windows"]["30"]["base"] == 0
    assert r["verdict"]["level"] == "empty"


def test_no_events_is_clean():
    r = sv.summarize([], 50, now=NOW)
    assert r["windows"]["30"]["survival_pct"] == 100.0
    assert r["verdict"]["level"] == "ok"
    assert "ни одной потери" in r["verdict"]["text"]


def test_lost_counted_once_per_account_and_base_includes_lost():
    ev = [_ev(1, "banned", 2), _ev(1, "deleted", 1), _ev(2, "banned", 20)]
    r = sv.summarize(ev, 98, now=NOW)
    w30, w7 = r["windows"]["30"], r["windows"]["7"]
    assert w30["lost"] == 2 and w30["base"] == 100 and w30["lost_pct"] == 2.0
    assert w7["lost"] == 1
    # Причина — первая потеря аккаунта в окне, а не последняя.
    assert {c["status"]: c["count"] for c in r["causes"]} == {"banned": 2}


def test_events_older_than_30_days_ignored():
    r = sv.summarize([_ev(1, "banned", 31)], 10, now=NOW)
    assert r["windows"]["30"]["lost"] == 0


def test_restricted_and_recovered():
    ev = [_ev(1, "spamblock", 5), _ev(1, "active", 2, old="spamblock"),
          _ev(2, "frozen", 10)]
    r = sv.summarize(ev, 10, now=NOW)
    w30 = r["windows"]["30"]
    assert w30["restricted"] == 2 and w30["recovered"] == 1 and w30["lost"] == 0
    assert r["windows"]["7"]["restricted"] == 1


def test_trend_better_and_worse():
    worse = sv.summarize([_ev(1, "banned", 1), _ev(2, "banned", 2), _ev(3, "banned", 9)],
                         100, now=NOW)
    assert worse["trend"] == {"this_week": 2, "prev_week": 1, "direction": "worse"}
    better = sv.summarize([_ev(3, "banned", 9), _ev(4, "banned", 10)], 100, now=NOW)
    assert better["trend"]["direction"] == "better"


def test_age_and_proxy_breakdown():
    ev = [_ev(1, "banned", 1, age_days=2, has_proxy=False),
          _ev(2, "banned", 1, age_days=15, has_proxy=True),
          _ev(3, "banned", 1, age_days=None, has_proxy=None)]
    r = sv.summarize(ev, 10, now=NOW)
    ages = {a["key"]: a["count"] for a in r["by_age"]}
    assert ages == {"fresh": 1, "young": 1, "mature": 0, "unknown": 1}
    assert r["by_proxy"] == {"with_proxy": 1, "no_proxy": 1, "unknown": 1}


def test_verdict_names_dominant_cause_only_when_it_dominates():
    no_proxy = [_ev(i, "banned", 1, has_proxy=False) for i in range(4)] + [_ev(9, "banned", 1)]
    v = sv.summarize(no_proxy, 20, now=NOW)["verdict"]
    assert v["level"] == "bad" and "без прокси" in v["text"]

    fresh = [_ev(i, "banned", 1, age_days=1) for i in range(4)]
    v = sv.summarize(fresh, 20, now=NOW)["verdict"]
    assert v["level"] == "bad" and "моложе 7 дней" in v["text"]

    mixed = [_ev(1, "banned", 1, age_days=1), _ev(2, "banned", 1, has_proxy=False),
             _ev(3, "banned", 1), _ev(4, "banned", 1)]
    v = sv.summarize(mixed, 20, now=NOW)["verdict"]
    assert v["level"] == "bad"
    assert "без прокси" not in v["text"] and "моложе" not in v["text"]


def test_verdict_warns_on_restrictions():
    ev = [_ev(i, "spamblock", 1) for i in range(3)]
    v = sv.summarize(ev, 20, now=NOW)["verdict"]
    assert v["level"] == "warn" and "Ограничений больше нормы" in v["text"]


def test_wired_into_api_and_screen():
    api = open(os.path.join(ROOT, "services", "mini_app_accounts.py"), encoding="utf-8").read()
    assert '"/api/miniapp/accounts/survival", accounts_survival' in api
    js = open(os.path.join(ROOT, "mini_app", "screens", "accounts_list.js"), encoding="utf-8").read()
    assert "loadAccSurvival();" in js and "/api/miniapp/accounts/survival" in js
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="accSurvival"' in html


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_fetch_survival_on_real_schema():
    import asyncio

    import asyncpg

    async def _run():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            async with pool.acquire() as c:
                await c.execute(
                    "CREATE TABLE IF NOT EXISTS tg_accounts (id BIGSERIAL PRIMARY KEY, "
                    "owner_id BIGINT, acc_status TEXT, proxy_id BIGINT, "
                    "added_at TIMESTAMPTZ DEFAULT now())")
                await c.execute(
                    "CREATE TABLE IF NOT EXISTS account_status_events (id BIGSERIAL PRIMARY KEY, "
                    "acc_id BIGINT NOT NULL, owner_id BIGINT, old_status TEXT, "
                    "new_status TEXT NOT NULL, is_death BOOLEAN DEFAULT FALSE, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                owner = 987654321
                await c.execute("DELETE FROM account_status_events WHERE owner_id=$1", owner)
                await c.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
                ids = [await c.fetchval(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, proxy_id, added_at) "
                    "VALUES ($1, $4, 'test', $2, $3, now() - INTERVAL '3 days') RETURNING id",
                    owner, st, px, f"+7999000{n:04d}") for n, (st, px) in enumerate(
                    (("active", None), ("active", None), ("banned", None), ("spamblock", None)))]
                await c.execute(
                    "INSERT INTO account_status_events(acc_id, owner_id, old_status, new_status, "
                    "created_at) VALUES ($1,$3,'active','banned', now() - INTERVAL '1 day'),"
                    "($2,$3,'active','spamblock', now() - INTERVAL '2 days'),"
                    "(424242,$3,'active','deleted', now() - INTERVAL '9 days')",
                    ids[2], ids[3], owner)
            r = await sv.fetch_survival(pool, owner)
            w30 = r["windows"]["30"]
            assert r["fleet_now"] == 3
            assert w30["lost"] == 2 and w30["base"] == 5 and w30["restricted"] == 1
            assert r["by_proxy"] == {"with_proxy": 0, "no_proxy": 1, "unknown": 1}
            assert r["trend"]["this_week"] == 1 and r["trend"]["prev_week"] == 1
        finally:
            await pool.close()

    asyncio.run(_run())
