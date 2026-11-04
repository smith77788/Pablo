"""«Повторить с правками»: та же операция заново, с поправленным текстом.

В карточке операции было только «Повторить» — доделать остаток той же
работы. Самое частое желание после прогона другое: выпустить ТО ЖЕ САМОЕ ещё
раз с другим текстом или паузой. Для этого приходилось искать экран запуска и
заново выбирать каналы, аккаунты и всё остальное.

Что держит этот файл:
* копия получает правки и проверяет их (пустой текст, чужой ключ, число вне
  границ — отказ, а не запуск);
* отметки ПРОШЛОГО прогона в копию не переходят: по `launched_bot_ids`,
  `broadcast_id` или `retry_of_op` исполнитель решил бы, что работа уже
  сделана, и копия не сделала бы ничего; расписание тоже не копируется;
* копия ставится общей дверью шины (тариф, окно двойного тапа);
* идущую операцию копировать нельзя, чужую — тем более.

Запросы новые, поэтому проверка идёт на настоящей базе (полная схема
накатывается фикстурой из conftest).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from services import op_params_view as opv

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990771, 990772

_PARAMS = {
    "text": "Старый текст поста", "channel_ids": [11, 12, 13], "delay": 30,
    "silent": False, "launched_bot_ids": [5], "broadcast_id": 77, "retry_of_op": 1,
    "repeat_interval_min": 60, "_recurring_fail_streak": 2, "recurring_next_op": 9,
}


# ── Чистая часть: какие поля правятся и что копируется ─────────────────────

def test_only_human_fields_are_editable():
    keys = {f["key"] for f in opv.editable("mass_publish", _PARAMS)}
    assert keys == {"text", "delay", "silent"}, keys


def test_the_clone_drops_every_mark_of_the_previous_run():
    out = opv.clone_params("mass_publish", _PARAMS, {"text": "Новый текст", "delay": 45})
    assert out["text"] == "Новый текст" and out["delay"] == 45
    assert out["channel_ids"] == [11, 12, 13], "цели обязаны копироваться как есть"
    for k in ("launched_bot_ids", "broadcast_id", "retry_of_op", "repeat_interval_min",
              "_recurring_fail_streak", "recurring_next_op"):
        assert k not in out, f"{k} перешёл в копию — она решит, что работа уже сделана"
    assert _PARAMS["text"] == "Старый текст поста", "исходный словарь испорчен"


@pytest.mark.parametrize("changes,word", [
    ({"text": "   "}, "пустым"),
    ({"channel_ids": [1]}, "не правится"),
    ({"delay": -1}, "от 0"),
    ({"delay": "много"}, "целое число"),
    ({"text": "x" * 5000}, "длиннее"),
])
def test_bad_edits_are_refused_in_russian(changes, word):
    with pytest.raises(ValueError) as e:
        opv.clone_params("mass_publish", _PARAMS, changes)
    assert word in str(e.value)


# ── Живая база: ручки ──────────────────────────────────────────────────────

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
        try:
            await pool.execute("DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            ids = {}
            # Идущая операция — ПОСТОРОННЯЯ работа: без ссылки на предка,
            # иначе она числилась бы повтором той же работы, что и «done».
            _other = {k: v for k, v in _PARAMS.items() if k != "retry_of_op"}
            for name, st, prm in (("done", "done", _PARAMS), ("running", "running", _other)):
                ids[name] = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items, label)"
                    " VALUES($1,'mass_publish',$2::jsonb,$3,3,'Пост в каналы') RETURNING id",
                    OWNER, json.dumps(prm), st)
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
                await pool.execute("DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                                   [OWNER, STRANGER])
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_clone_runs_the_same_work_with_the_edits():
    async def body(client, pool, ids):
        src = ids["done"]
        ed = await (await client.get(f"/api/miniapp/operation/{src}/editable")).json()
        r = await client.post(f"/api/miniapp/operation/{src}/clone",
                              json={"changes": {"text": "Новый текст", "delay": 45}})
        res = await r.json()
        row = await pool.fetchrow(
            "SELECT op_type, params, status, total_items, label, owner_id FROM operation_queue WHERE id=$1",
            res.get("new_id"))
        return ed, r.status, dict(row) if row else None

    ed, status, row = _run(body)
    assert ed["can_clone"] is True and {f["key"] for f in ed["fields"]} == {"text", "delay", "silent"}
    assert any(c["label"] == "Каналы" for c in ed["context"]), "не сказано, по каким целям пойдёт копия"
    assert status == 200 and row, "копия не поставилась"
    p = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"])
    assert p["text"] == "Новый текст" and p["delay"] == 45 and p["channel_ids"] == [11, 12, 13]
    assert "launched_bot_ids" not in p and "broadcast_id" not in p and "retry_of_op" not in p
    assert "repeat_interval_min" not in p, "копия завела бы второе расписание"
    assert row["op_type"] == "mass_publish" and row["owner_id"] == OWNER
    assert row["total_items"] == 3 and "копия #" in row["label"]


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_a_running_or_foreign_operation_is_not_cloned():
    async def body(client, pool, ids):
        running = await client.post(f"/api/miniapp/operation/{ids['running']}/clone", json={})
        foreign = await client.post(f"/api/miniapp/operation/{ids['done']}/clone", json={},
                                    headers={"X-Uid": str(STRANGER)})
        foreign_ed = await client.get(f"/api/miniapp/operation/{ids['done']}/editable",
                                      headers={"X-Uid": str(STRANGER)})
        bad = await client.post(f"/api/miniapp/operation/{ids['done']}/clone",
                                json={"changes": {"text": ""}})
        n = await pool.fetchval("SELECT COUNT(*) FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                                [OWNER, STRANGER])
        return running.status, foreign.status, foreign_ed.status, bad.status, n

    running, foreign, foreign_ed, bad, n = _run(body)
    assert running == 409, "копию идущей операции поставили"
    assert foreign == 404 and foreign_ed == 404, "чужую операцию видно или можно скопировать"
    assert bad == 400
    assert n == 2, "что-то поставилось в очередь вопреки отказу"


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_a_second_copy_waits_for_the_first():
    """Двойное нажатие «Запустить копию» не ставит две копии параллельно.

    Та же защита, что у повтора (operation_bus.retry_blocker, коммит
    «Повтор операции не запускает ту же работу дважды параллельно»): исходная
    операция от копии не меняется, кнопка остаётся на месте, и вторая копия
    пошла бы по тем же целям одновременно с первой.
    """
    async def body(client, pool, ids):
        src = ids["done"]
        first = await client.post(f"/api/miniapp/operation/{src}/clone",
                                  json={"changes": {"text": "Первая копия"}})
        first_id = (await first.json())["new_id"]
        second = await client.post(f"/api/miniapp/operation/{src}/clone",
                                   json={"changes": {"text": "Вторая копия"}})
        ed = await (await client.get(f"/api/miniapp/operation/{src}/editable")).json()
        link = await pool.fetchval(
            "SELECT params->>'cloned_from_op' FROM operation_queue WHERE id=$1", first_id)
        # Первая закончилась — копировать снова можно.
        await pool.execute("UPDATE operation_queue SET status='done' WHERE id=$1", first_id)
        third = await client.post(f"/api/miniapp/operation/{src}/clone",
                                  json={"changes": {"text": "Третья копия"}})
        return first.status, second.status, (await second.json()), ed, link, third.status, src

    first, second, second_body, ed, link, third, src = _run(body)
    assert first == 200
    assert second == 409, "вторая копия пошла параллельно первой"
    assert "уже идёт" in second_body.get("error", "") + second_body.get("message", "")
    assert ed["can_clone"] is False and "уже идёт" in ed["why"]
    assert link == str(src), "у копии нет ссылки на источник"
    assert third == 200, "после итога первой копию снова запустить нельзя"


def test_the_card_offers_it():
    from tests.miniapp_source import js_function
    detail = js_function("openOpDetail")
    assert "openOpClone(" in detail and "Повторить с правками" in detail
    submit = js_function("submitOpClone")
    assert "/clone'" in submit and "changes" in submit
