"""Фоновые проверки сущностей не жгут аккаунты-наблюдатели.

Владелец (13.11.2026): после проверок «Ограничения и теневой бан» и
«Гео-доступность» (вручную и по расписанию раз в 12 часов) аккаунты флота,
которые смотрели на наши каналы, чаты и ботов, получают от @SpamBot
ограничение «жалоба на незаконные материалы» — нельзя создавать каналы и
писать в публичные группы. Система от этого не защищала:

* пул наблюдателей сдвигался на каждом прогоне (`op_id % len`) — за несколько
  кругов сеть «трогал» весь флот;
* /start каждому боту слал аккаунт флота — единственное исходящее сообщение
  проверки;
* ограниченные аккаунты оставались наблюдателями, а расписание крутилось
  дальше, сколько бы аккаунтов ни пострадало.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from services import entity_restriction_check as erc
from services import observer_guard as og


def _accs(*ids):
    return [{"id": i} for i in ids]


def test_observers_are_the_same_every_run_and_skip_channel_admins():
    obs = _accs(9, 3, 7, 5, 1)
    a = og.stable_pick(obs, 2, avoid_ids={1})
    b = og.stable_pick(list(reversed(obs)), 2, avoid_ids={1})
    assert [x["id"] for x in a] == [x["id"] for x in b] == [3, 5]
    assert [x["id"] for x in og.stable_pick(_accs(1, 2), 2, avoid_ids={1, 2})] == [1, 2], (
        "без запасных аккаунтов ведущие каналы всё же берутся — проверка не встаёт")


def test_executor_pool_no_longer_rotates_with_the_operation_number():
    import inspect
    from services import op_worker
    src = inspect.getsource(op_worker._exec_check_owned_restrictions)
    assert "% len(observers)" not in src, "пул снова сдвигается по op_id — флот размазывается"
    assert "_og.stable_pick(" in src and "do_bot_ping=False" in src
    assert "_og.observer_where(" in src
    geo = inspect.getsource(op_worker._exec_check_bot_geoban)
    assert "_og.stable_pick(" in geo and "_og.observer_where(" in geo


def test_observer_filter_excludes_restricted_and_dead():
    sql = og.observer_where("a")
    for st in ("spamblock", "frozen", "banned", "session_expired"):
        assert f"'{st}'" in sql
    assert "a.acc_status" in sql


def test_dead_bot_token_is_unreachable_without_any_account():
    v = erc.classify("bot", {"has_username": True, "resolved": True, "token_alive": False})
    assert v["status"] == erc.STATUS_UNREACHABLE and "токен" in v["reason_ru"]
    v = erc.classify("bot", {"has_username": True, "resolved": True, "token_alive": True,
                             "found_in_search": True})
    assert v["status"] == erc.STATUS_CLEAN


class _P:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.sql = []

    async def fetch(self, sql, *a):
        if self.fail:
            raise RuntimeError("db down")
        self.sql.append((sql, a))
        return self.rows

    async def execute(self, sql, *a):
        self.sql.append((sql, a))
        return "UPDATE 1"


def test_trip_needs_a_previous_run_and_reads_events_after_it():
    assert asyncio.run(og.tripped(_P(rows=[{"acc_id": 1}]), {})) == []
    params = {}
    og.remember_run(params, [5, 3, 5])
    assert params[og.LAST_OBSERVERS_KEY] == [3, 5]
    pool = _P(rows=[{"acc_id": 3, "new_status": "spamblock", "username": "obs3"}])
    hit = asyncio.run(og.tripped(pool, params))
    assert hit and hit[0]["acc_id"] == 3
    sql, args = pool.sql[0]
    assert "'spamblock'" in sql and "'session_expired'" not in sql, (
        "умершая сессия — не след проверки, расписание из-за неё не снимаем")
    assert args[0] == [3, 5] and isinstance(args[1], datetime)
    assert asyncio.run(og.tripped(_P(fail=True), params)) == [], "блип базы — не стоп"


def test_trip_summary_is_russian_and_names_the_accounts():
    text = og.trip_summary([{"acc_id": 3, "new_status": "spamblock", "username": "obs3"}],
                           "Гео-доступность")
    assert "остановлена" in text and "@obs3" in text and "спам-блок" in text
    assert not any("a" <= ch.lower() <= "z" for ch in text.replace("obs3", "")
                   .replace("<b>", "").replace("</b>", "").replace("Telegram", ""))


def test_stop_schedule_writes_the_row_too():
    pool = _P()
    params = {"repeat_interval_min": 720}
    asyncio.run(og.stop_schedule(pool, 42, params))
    assert params["repeat_interval_min"] == 0
    sql, args = pool.sql[0]
    assert "repeat_interval_min" in sql and args == (42,)


# ── Исполнители: предохранитель и отметки ────────────────────────────────────

from tests.test_bot_geoban_check import _GeoPool, _geo_acc, _run_geo_exec  # noqa: E402


def test_geo_check_stops_its_schedule_when_an_observer_got_restricted(monkeypatch):
    async def _hit(pool, params):
        return [{"acc_id": 1, "new_status": "spamblock", "username": "ru1"}]
    stopped = []

    async def _stop(pool, op_id, params):
        stopped.append(op_id)
        params["repeat_interval_min"] = 0
    monkeypatch.setattr(og, "tripped", _hit)
    monkeypatch.setattr(og, "stop_schedule", _stop)
    pool = _GeoPool(channels=[{"channel_id": 1, "username": "news", "title": "Н",
                               "type": "channel"}], bots=[],
                    accounts=[_geo_acc(1, "RU"), _geo_acc(2, "DE")])
    params = {"repeat_interval_min": 720}
    res, calls, _ = _run_geo_exec(pool, {}, params=params)
    assert res["status"] == "failed" and res.get("stop_reason") == "observers_restricted"
    assert calls == [], "проверка пошла, хотя наблюдатель уже пострадал"
    assert stopped == [7] and params["repeat_interval_min"] == 0


def test_geo_check_marks_its_observers_for_a_spambot_recheck(monkeypatch):
    marked = []

    async def _mark(pool, ids):
        marked.extend(ids)
    monkeypatch.setattr(og, "mark_for_recheck", _mark)
    pool = _GeoPool(channels=[{"channel_id": 1, "username": "news", "title": "Н",
                               "type": "channel"}], bots=[],
                    accounts=[_geo_acc(1, "RU"), _geo_acc(2, "DE")])
    params = {"repeat_interval_min": 720}
    res, calls, _ = _run_geo_exec(pool, {}, params=params)
    assert res["status"] == "done"
    assert sorted(marked) == [1, 2]
    assert params[og.LAST_OBSERVERS_KEY] == [1, 2] and params.get(og.LAST_RUN_KEY)


def test_observers_query_filters_restricted_accounts():
    pool = _GeoPool(channels=[{"channel_id": 1, "username": "news", "title": "Н",
                               "type": "channel"}], bots=[],
                    accounts=[_geo_acc(1, "RU"), _geo_acc(2, "DE")])
    _run_geo_exec(pool, {})
    acc_q = next(q for q in pool.fetch_queries if "from tg_accounts" in q)
    assert "'spamblock'" in acc_q and "'frozen'" in acc_q


def test_recheck_puts_observers_first_in_the_spambot_queue():
    src = open(__import__("services.account_health", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    cyc = src.split("async def _run_spambot_check_cycle(", 1)[1].split("\nasync def ", 1)[0]
    assert "COALESCE(a.last_spambot_check_at, '2000-01-01') ASC" in cyc, (
        "NULL-отметка больше не ставит наблюдателя первым — предохранитель ослеп")


# ── Живая база: запрос предохранителя принимается Postgres ─────────────────

import os  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres (INFRAGRAM_TEST_DSN)")
def test_live_trip_query_and_recheck():
    import asyncpg
    import glob
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    async def _go():
        conn = await asyncpg.connect(DSN, server_settings={"lock_timeout": "3000"})
        for f in [os.path.join(root, "schema.sql")] + sorted(
                glob.glob(os.path.join(root, "schema_v*.sql")),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            owner = 995604
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
            ids = [await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, "
                "last_spambot_check_at) VALUES($1,$2,'s-xxxxxxxxxxxx',TRUE,'active',now()) "
                "RETURNING id", owner, f"+7999{os.urandom(3).hex()}") for _ in range(2)]
            params = {}
            og.remember_run(params, ids)
            params[og.LAST_RUN_KEY] = (datetime.now(timezone.utc)
                                       - timedelta(minutes=5)).isoformat()
            assert await og.tripped(pool, params) == []
            from services import account_status
            await account_status.set_status(pool, ids[0], "spamblock", source="test")
            hit = await og.tripped(pool, params)
            assert [h["acc_id"] for h in hit] == [ids[0]]
            await og.mark_for_recheck(pool, ids)
            n = await pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE id = ANY($1::int[]) "
                                    "AND last_spambot_check_at IS NULL", ids)
            assert n == 2
            q = ("SELECT COUNT(*) FROM tg_accounts a WHERE a.owner_id=$1 AND "
                 + og.observer_where("a"))
            assert await pool.fetchval(q, owner) == 1
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
        finally:
            await pool.close()
    asyncio.run(_go())
