"""Экран «Запланировано»: всё отложенное и повторяемое — в одном месте, и его можно перенести.

ЧТО БЫЛО. Отложенные рассылки бота были видны только в карточке каждого бота
по отдельности, отложенные публикации — в общей очереди операций вперемешку с
историей, повторяемые круги как расписание не показывались нигде. Перенести
время было нельзя: только отменить и завести заново.

Что держит этот файл:
* сводка собирает и рассылки ботов, и отложенные/повторяемые операции владельца,
  и НЕ показывает чужое;
* перенос времени и смена текста/повтора трогают только ещё не занятую строку
  (`pending`): строку, которую уже взял исполнитель, правка не сдвигает;
* повтор разрешён только типам, которые исполнитель умеет зацикливать;
* «повторить по расписанию» ставит операцию общей дверью шины с
  `scheduled_for`, а второе нажатие не заводит вторую цепочку;
* экран подключён: плитка в меню, скрипт, ручки.

Запросы новые, поэтому поведение проверяется на настоящей базе.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib

import pytest

from services import mini_app_planner as P

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990881, 990882
BOT, STRANGER_BOT = 990881001, 990882001
ROOT = pathlib.Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


# ── Чистая часть ────────────────────────────────────────────────────────────

def test_time_must_be_in_the_future_and_is_rounded_up_to_a_minute():
    now = dt.datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    got = P.parse_when("2026-11-23T13:30:15Z", now=now)
    assert got == dt.datetime(2026, 11, 23, 13, 31, tzinfo=UTC)
    for bad in ("", "вчера", "2026-11-23T11:00:00Z", "2027-11-23T11:00:00Z"):
        with pytest.raises(P.PlanError):
            P.parse_when(bad, now=now)


def test_naive_time_is_utc_not_server_local():
    now = dt.datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert P.parse_when("2026-11-23T15:00", now=now).tzinfo is not None


@pytest.mark.parametrize("raw,want", [(None, 0), ("", 0), ("none", 0), (0, 0), (-5, 0),
                                      (60, 60), ("1440", 1440), (43200, 43200)])
def test_repeat_bounds(raw, want):
    assert P.parse_repeat(raw) == want


@pytest.mark.parametrize("raw", [30, 43201, "часто"])
def test_repeat_out_of_bounds_is_refused_in_russian(raw):
    with pytest.raises(P.PlanError) as e:
        P.parse_repeat(raw)
    assert any(w in str(e.value) for w in ("час", "30 дней", "целое"))


def test_a_running_operation_is_not_editable_but_a_waiting_one_is():
    now = dt.datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    base = {"id": 1, "op_type": "mass_publish", "label": "Пост",
            "params": json.dumps({"text": "Привет", "repeat_interval_min": 360}),
            "total_items": 3, "done_items": 1, "error_msg": ""}
    waiting = P.operation_item({**base, "status": "pending",
                                "scheduled_for": now + dt.timedelta(hours=2)}, now=now)
    assert waiting["editable"] and waiting["cancellable"] and waiting["repeat_min"] == 360
    assert waiting["text"] == "Привет"
    running = P.operation_item({**base, "status": "running", "scheduled_for": None,
                                "started_at": now}, now=now)
    assert not running["editable"] and not running["cancellable"]


# ── Живая база: ручки ───────────────────────────────────────────────────────

def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M
        from services import operation_bus as B

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig_uid, orig_plan = M._get_uid, B._enforce_min_plan

        async def _no_plan_gate(*a, **k):
            return False

        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))
        B._enforce_min_plan = _no_plan_gate

        async def clean():
            await pool.execute("DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.execute("DELETE FROM scheduled_broadcasts WHERE created_by = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                               [BOT, STRANGER_BOT])

        try:
            await clean()
            for bot, who in ((BOT, OWNER), (STRANGER_BOT, STRANGER)):
                await pool.execute(
                    "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
                    "VALUES($1,$2,$3,TRUE,$4)", f"tok-{bot}", bot, f"bot{bot}", who)
            soon = dt.datetime.now(UTC) + dt.timedelta(hours=3)
            ids = {}
            for name, bot, who, st in (("mine", BOT, OWNER, "pending"),
                                       ("busy", BOT, OWNER, "processing"),
                                       ("theirs", STRANGER_BOT, STRANGER, "pending")):
                ids[name] = await pool.fetchval(
                    "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by, status)"
                    " VALUES($1,'Скидка завтра',$2,$3,$4) RETURNING id", bot, soon, who, st)
            for name, st, sched, prm in (
                ("op_wait", "pending", soon, {"text": "Пост", "repeat_interval_min": 0}),
                ("op_loop", "running", None, {"text": "Круг", "repeat_interval_min": 360}),
                ("op_plain", "pending", None, {"text": "Просто в очереди"}),
                ("op_done", "done", None, {}),
            ):
                ids[name] = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items,"
                    " label, scheduled_for) VALUES($1,$2,$3::jsonb,$4,3,'Пост в каналы',$5) RETURNING id",
                    OWNER, "check_accounts_health" if name == "op_done" else "mass_publish",
                    json.dumps(prm), st, sched)
            ids["op_odd"] = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items,"
                " label, scheduled_for) VALUES($1,'mass_invite','{}'::jsonb,'pending',3,'Инвайт',$2)"
                " RETURNING id", OWNER, soon)
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool, ids)
            finally:
                await client.close()
        finally:
            M._get_uid, B._enforce_min_plan = orig_uid, orig_plan
            try:
                await clean()
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def _future(hours: float = 5) -> str:
    return (dt.datetime.now(UTC) + dt.timedelta(hours=hours)).isoformat()


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_summary_shows_my_scheduled_work_and_not_someone_elses():
    async def body(client, pool, ids):
        r = await client.get("/api/miniapp/planner")
        assert r.status == 200, await r.text()
        d = await r.json()
        up = {(i["kind"], i["id"]) for i in d["upcoming"]}
        assert ("broadcast", ids["mine"]) in up
        assert ("broadcast", ids["busy"]) in up
        assert ("broadcast", ids["theirs"]) not in up, "в сводку попала чужая рассылка"
        assert ("operation", ids["op_wait"]) in up, "отложенная операция не видна"
        assert ("operation", ids["op_loop"]) in up, "идущий повторяемый круг не виден"
        assert ("operation", ids["op_plain"]) not in up, (
            "операция без времени и без повтора — просто очередь, не расписание")
        assert d["counts"]["recurring"] >= 1
        busy = next(i for i in d["upcoming"] if i["kind"] == "broadcast" and i["id"] == ids["busy"])
        assert not busy["editable"], "занятую планировщиком рассылку предлагают править"
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_a_bot_broadcast_can_be_moved_and_retexted_until_it_is_taken():
    async def body(client, pool, ids):
        when = _future(26)
        r = await client.post(f"/api/miniapp/planner/broadcast/{ids['mine']}",
                              json={"run_at": when, "repeat_min": 1440, "text": "Новый текст"})
        assert r.status == 200, await r.text()
        row = await pool.fetchrow("SELECT execute_at, repeat_interval_min, message_text "
                                  "FROM scheduled_broadcasts WHERE id=$1", ids["mine"])
        assert row["message_text"] == "Новый текст" and row["repeat_interval_min"] == 1440
        assert abs((row["execute_at"] - dt.datetime.fromisoformat(when)).total_seconds()) <= 60

        r = await client.post(f"/api/miniapp/planner/broadcast/{ids['busy']}",
                              json={"run_at": when, "repeat_min": 0})
        assert r.status == 409, "правка сдвинула рассылку, которую уже отправляет планировщик"

        r = await client.post(f"/api/miniapp/planner/broadcast/{ids['theirs']}",
                              json={"run_at": when, "repeat_min": 0})
        assert r.status == 404
        st = await pool.fetchval("SELECT message_text FROM scheduled_broadcasts WHERE id=$1",
                                 ids["theirs"])
        assert st == "Скидка завтра", "правка дотянулась до чужой рассылки"

        r = await client.post(f"/api/miniapp/planner/broadcast/{ids['mine']}",
                              json={"run_at": "2020-01-01T00:00:00Z"})
        assert r.status == 400
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_a_waiting_operation_can_be_moved_but_a_running_one_cannot():
    async def body(client, pool, ids):
        when = _future(30)
        r = await client.post(f"/api/miniapp/planner/operation/{ids['op_wait']}",
                              json={"run_at": when, "repeat_min": 720})
        assert r.status == 200, await r.text()
        row = await pool.fetchrow("SELECT scheduled_for, params FROM operation_queue WHERE id=$1",
                                  ids["op_wait"])
        assert json.loads(row["params"])["repeat_interval_min"] == 720
        assert row["scheduled_for"] > dt.datetime.now(UTC) + dt.timedelta(hours=29)

        r = await client.post(f"/api/miniapp/planner/operation/{ids['op_loop']}",
                              json={"run_at": when, "repeat_min": 60})
        assert r.status == 409, "перенос сдвинул операцию из-под исполнителя"

        r = await client.post(f"/api/miniapp/planner/operation/{ids['op_odd']}",
                              json={"run_at": when, "repeat_min": 60})
        assert r.status == 400, "повтор разрешили типу, который исполнитель не зацикливает"

        r = await client.post(f"/api/miniapp/planner/operation/{ids['op_wait']}",
                              json={"run_at": when}, headers={"X-Uid": str(STRANGER)})
        assert r.status == 404
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_stopping_the_repeat_leaves_the_current_round_alone():
    async def body(client, pool, ids):
        r = await client.post(f"/api/miniapp/planner/operation/{ids['op_loop']}/stop_repeat")
        assert r.status == 200, await r.text()
        row = await pool.fetchrow("SELECT status, params FROM operation_queue WHERE id=$1",
                                  ids["op_loop"])
        assert row["status"] == "running", "снятие повтора остановило идущий круг"
        assert json.loads(row["params"])["repeat_interval_min"] == 0
        r = await client.post(f"/api/miniapp/planner/operation/{ids['op_loop']}/stop_repeat")
        assert r.status == 409
    _run(body)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_a_finished_operation_can_be_put_on_a_schedule_once():
    async def body(client, pool, ids):
        src = ids["op_done"]
        cand = await (await client.get("/api/miniapp/planner/candidates")).json()
        assert src in [i["id"] for i in cand["items"]]

        when = _future(4)
        r = await client.post(f"/api/miniapp/planner/operation/{src}/schedule",
                              json={"run_at": when, "repeat_min": 720})
        res = await r.json()
        assert r.status == 200, res
        row = await pool.fetchrow(
            "SELECT op_type, status, scheduled_for, params, owner_id FROM operation_queue WHERE id=$1",
            res["new_id"])
        prm = json.loads(row["params"])
        assert row["owner_id"] == OWNER and row["op_type"] == "check_accounts_health"
        assert row["status"] in ("pending", "scheduled")
        assert row["scheduled_for"] > dt.datetime.now(UTC) + dt.timedelta(hours=3)
        assert prm["repeat_interval_min"] == 720 and prm["cloned_from_op"] == src

        again = await client.post(f"/api/miniapp/planner/operation/{src}/schedule",
                                  json={"run_at": _future(6), "repeat_min": 720})
        assert again.status == 409, "второе нажатие завело вторую цепочку той же работы"

        not_done = await client.post(f"/api/miniapp/planner/operation/{ids['op_plain']}/schedule",
                                     json={"run_at": when, "repeat_min": 0})
        assert not_done.status == 409

        d = await (await client.get("/api/miniapp/planner")).json()
        assert ("operation", res["new_id"]) in {(i["kind"], i["id"]) for i in d["upcoming"]}
    _run(body)


# ── Экран подключён ─────────────────────────────────────────────────────────

def test_screen_is_reachable_and_wired_to_the_routes():
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "mini_app" / "screens" / "planner.js").read_text(encoding="utf-8")
    assert '<script src="screens/planner.js"></script>' in html
    assert 'onclick="openPlanner()"' in html and ">Запланировано<" in html
    for url in ("/api/miniapp/planner'", "/api/miniapp/planner/broadcast/",
                "/api/miniapp/planner/operation/", "/stop_repeat", "/schedule'",
                "/api/miniapp/planner/candidates"):
        assert url in js, f"экран не зовёт {url}"
    from services.task_sections import section_of_tile
    assert section_of_tile("Запланировано").key == "messages"
