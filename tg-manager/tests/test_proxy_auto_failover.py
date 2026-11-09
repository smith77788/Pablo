"""Автопереезд с мёртвого прокси: только на свободные живые, сначала та же страна."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from services import proxy_watchdog as W

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_same_country_first_then_any():
    plan = W.plan_auto_failover([(1, "de"), (2, "NL")], [(10, "US"), (11, "DE")])
    assert plan == {"moves": [(1, 11), (2, 10)], "stranded": []}


def test_never_two_accounts_on_one_proxy():
    plan = W.plan_auto_failover([(1, "DE"), (2, "DE"), (3, "DE")], [(10, "DE"), (11, "US")])
    pids = [p for _a, p in plan["moves"]]
    assert len(pids) == len(set(pids)) == 2
    assert plan["stranded"] == [3]


def test_no_free_proxies_strands_everyone():
    assert W.plan_auto_failover([(1, "DE")], []) == {"moves": [], "stranded": [1]}
    assert W.plan_auto_failover([], [(10, "DE")]) == {"moves": [], "stranded": []}


def test_unknown_country_takes_any_free():
    assert W.plan_auto_failover([(1, None)], [(10, "DE")])["moves"] == [(1, 10)]


def test_failover_note_texts():
    assert "все 3" in W.build_failover_note(3, 0)
    assert "1 остались" in W.build_failover_note(2, 1)
    assert "свободных живых прокси нет" in W.build_failover_note(0, 2)


class _Pool:
    def __init__(self, settings):
        self.settings = settings

    async def fetchval(self, q, *a):
        return self.settings


def test_setting_is_off_by_default_and_reads_json():
    assert asyncio.run(W.auto_failover_enabled(_Pool(None), 1)) is False
    assert asyncio.run(W.auto_failover_enabled(_Pool("{}"), 1)) is False
    assert asyncio.run(W.auto_failover_enabled(
        _Pool(json.dumps({"proxy_auto_failover": True})), 1)) is True


def test_setting_is_wired_into_settings_screen():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="set-proxy-autofailover"' in html
    assert "setChk('set-proxy-autofailover', backend.proxy_auto_failover, false)" in html
    assert "proxy_auto_failover: !!document.getElementById('set-proxy-autofailover')" in html


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_watchdog_moves_accounts_on_real_schema(monkeypatch):
    import asyncpg

    owner = 987650001

    async def _run():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            async with pool.acquire() as c:
                await c.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
                await c.execute("DELETE FROM user_proxies WHERE owner_id=$1", owner)
                await c.execute("DELETE FROM platform_users WHERE user_id=$1", owner)
                await c.execute(
                    "INSERT INTO platform_users(user_id, settings_json) VALUES ($1, $2)",
                    owner, json.dumps({"proxy_auto_failover": True}))

                async def _proxy(url, geo, alive=True, streak=0):
                    return await c.fetchval(
                        "INSERT INTO user_proxies(owner_id, proxy_url, geo_country, is_active, "
                        "is_alive, consecutive_failures) VALUES ($1,$2,$3,TRUE,$4,$5) RETURNING id",
                        owner, url, geo, alive, streak)
                dead = await _proxy("socks5://9.9.9.1:1080", "DE", True, W.FAIL_STREAK_TO_ALERT - 1)
                free_de = await _proxy("socks5://9.9.9.2:1080", "DE")
                free_us = await _proxy("socks5://9.9.9.3:1080", "US")
                busy = await _proxy("socks5://9.9.9.4:1080", "DE")
                accs = []
                for n, pid in enumerate((dead, dead, dead, busy)):
                    accs.append(await c.fetchval(
                        "INSERT INTO tg_accounts(owner_id, phone, session_str, proxy_id, is_active) "
                        "VALUES ($1,$2,'s',$3,TRUE) RETURNING id",
                        owner, f"+7999111{n:04d}", pid))

            async def _fake(url, timeout_s=10.0):
                return {"alive": "9.9.9.1" not in url, "latency_ms": 10}

            sent = []

            async def _fake_notify(pool_, bot_, owner_id, proxy_id, text):
                sent.append(text)
                return True

            monkeypatch.setattr(W, "check_proxy_alive", _fake)
            monkeypatch.setattr(W, "_notify", _fake_notify)
            monkeypatch.setattr("services.proxy_hygiene.proxy_plain_url", lambda u: u)
            await W.check_once(pool, object())

            rows = await pool.fetch(
                "SELECT id, proxy_id FROM tg_accounts WHERE owner_id=$1 ORDER BY id", owner)
            got = [r["proxy_id"] for r in rows]
            assert got == [free_de, free_us, dead, busy]
            assert len(sent) == 1 and "2 аккаунт(ов) переселены" in sent[0]
            assert "1 остались" in sent[0]
        finally:
            await pool.close()

    asyncio.run(_run())
