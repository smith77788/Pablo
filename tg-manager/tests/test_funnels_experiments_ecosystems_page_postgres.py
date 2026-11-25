"""Воронки, эксперименты и экосистемы листаются — на живой базе.

РАЗРЫВ. Три списка стояли на жёстком потолке и молчали о нём, причём у каждого
сортировка прятала именно то, за чем приходят:

* воронки — `LIMIT 30` с сортировкой по числу активных подписчиков, то есть
  воронка БЕЗ подписчиков (та самая, которую надо починить) уходила в конец и
  за потолок;
* эксперименты — `LIMIT 50` по дате, и плитка раздела писала «N экспериментов»
  по длине страницы: неверное число, выданное как факт;
* экосистемы — `LIMIT 30` по времени правки, голым массивом: всё, к чему давно
  не прикасались, для экрана не существовало.

Живая база нужна, а не заглушка: у всех трёх появились `LIMIT $2 OFFSET $3` и
запрос-счётчик, а у воронок и экспериментов счёт идёт по условию с джойном к
`managed_bots` — разъехавшееся условие на заглушке было бы невидимо.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995911
STRANGER = 995912
BOT = 880099911
BOT_STRANGER = 880099912


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    await pool.execute(
        "DELETE FROM experiments WHERE bot_id = ANY($1::bigint[])", [BOT, BOT_STRANGER])
    await pool.execute(
        "DELETE FROM funnels WHERE bot_id = ANY($1::bigint[])", [BOT, BOT_STRANGER])
    await pool.execute(
        "DELETE FROM ecosystems WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
    await pool.execute(
        "DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", [BOT, BOT_STRANGER])


async def _seed(pool):
    await _clean(pool)
    for bot_id, owner in ((BOT, OWNER), (BOT_STRANGER, STRANGER)):
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,$3,TRUE,$4)",
            f"1234:page-test-{bot_id}", bot_id, f"page_test_{bot_id}", owner)
    for i in range(7):
        await pool.execute(
            "INSERT INTO funnels(bot_id, name, trigger_type, is_active) "
            "VALUES($1,$2,'start',TRUE)",
            BOT, f"Воронка {i}")
        await pool.execute(
            "INSERT INTO experiments(bot_id, name, experiment_type, status) "
            "VALUES($1,$2,'start_message','draft')", BOT, f"Опыт {i}")
        await pool.execute(
            "INSERT INTO ecosystems(owner_id, name, ecosystem_type, status) "
            "VALUES($1,$2,'network','active')", OWNER, f"Сеть {i}")
    # Чужое: ни в выдачу, ни в счёт.
    await pool.execute(
        "INSERT INTO funnels(bot_id, name, trigger_type, is_active) "
        "VALUES($1,'Чужая воронка','start',TRUE)",
        BOT_STRANGER)
    await pool.execute(
        "INSERT INTO experiments(bot_id, name, experiment_type, status) "
        "VALUES($1,'Чужой опыт','start_message','draft')", BOT_STRANGER)
    await pool.execute(
        "INSERT INTO ecosystems(owner_id, name, ecosystem_type, status) "
        "VALUES($1,'Чужая сеть','network','active')", STRANGER)


def _ask(monkeypatch, module: str, route: str, query: str, uid: int = OWNER) -> dict:
    import asyncpg
    import importlib
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api

    mod = importlib.import_module(f"services.{module}")
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: uid)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _seed(pool)
            app = web.Application()
            mod.setup_routes(app, pool)
            handler = next(
                r.handler for r in app.router.routes()
                if getattr(r.resource, "canonical", "") == route)
            resp = await handler(make_mocked_request("GET", route + query))
            return json.loads(resp.body)
        finally:
            await _clean(pool)
            await pool.close()

    return _run(_go())


_CASES = (
    ("mini_app_funnels", "/api/miniapp/funnels", "funnels", "Чужая воронка"),
    ("mini_app_growth", "/api/miniapp/experiments", "experiments", "Чужой опыт"),
    ("mini_app_networks", "/api/miniapp/ecosystems", "ecosystems", "Чужая сеть"),
)


@pytest.mark.parametrize("module,route,key,alien", _CASES)
def test_the_page_names_the_whole_count(monkeypatch, module, route, key, alien):
    d = _ask(monkeypatch, module, route, "?limit=3&offset=0")
    assert len(d[key]) == 3, d
    assert d["page"]["total"] == 7, "итог обязан считаться по всей выборке владельца"
    assert d["page"]["has_more"] is True


@pytest.mark.parametrize("module,route,key,alien", _CASES)
def test_the_pages_together_give_everything_once(monkeypatch, module, route, key, alien):
    names = []
    for off in (0, 3, 6):
        d = _ask(monkeypatch, module, route, f"?limit=3&offset={off}")
        names += [r["name"] for r in d[key]]
    assert len(names) == 7, names
    assert len(set(names)) == 7, "страницы перекрылись — часть строк видна дважды"
    last = _ask(monkeypatch, module, route, "?limit=3&offset=6")
    assert last["page"]["has_more"] is False


@pytest.mark.parametrize("module,route,key,alien", _CASES)
def test_another_owner_is_neither_shown_nor_counted(monkeypatch, module, route, key, alien):
    d = _ask(monkeypatch, module, route, "?limit=50")
    assert d["page"]["total"] == 7
    assert alien not in [r["name"] for r in d[key]]
