"""«Найдено у вас»: поиск по своим объектам из строки поиска каталога.

Проверяется:
* шаблон ILIKE экранирует %, _ и \\ — запрос «50%» ищет «50%», а не всё;
* каждый запрос группы фильтрует по владельцу и добивает сортировку по id;
* у группы честный счёт всех совпадений, а не числа показанных;
* сбой одной группы не роняет поиск;
* короткий запрос — 400; ручка, дверь в мини-аппе и открытие каждого типа на месте;
* на живой схеме все запросы групп выполняются и чужое не находится.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from services import mini_app_find as fm

from tests.miniapp_source import api_routes, api_source, miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_like_pattern_escapes_wildcards():
    assert fm.like_pattern("50%") == "%50\\%%"
    assert fm.like_pattern("a_b") == "%a\\_b%"
    assert fm.like_pattern("c\\d") == "%c\\\\d%"


def test_clean_query_trims_and_caps():
    assert fm.clean_query("  Аня   Петрова ") == "Аня Петрова"
    assert len(fm.clean_query("x" * 500)) == fm.MAX_QUERY


def test_every_group_is_owner_scoped_and_tiebroken():
    for key, _label, rows_sql, count_sql in fm._GROUPS:
        owner_col = "added_by=$1" if key == "bots" else "owner_id=$1"
        assert owner_col in rows_sql and owner_col in count_sql, key
        assert re.search(r"ORDER BY .*\bid\b", rows_sql), key
        assert "ESCAPE" in rows_sql and "ESCAPE" in count_sql, key


class _Pool:
    def __init__(self, rows_by_table, totals, fail=()):
        self.rows, self.totals, self.fail = rows_by_table, totals, fail

    def _table(self, sql):
        return re.search(r"FROM (\w+)", sql).group(1)

    async def fetch(self, sql, *a):
        t = self._table(sql)
        if t in self.fail:
            raise RuntimeError("relation does not exist")
        return self.rows.get(t, [])

    async def fetchval(self, sql, *a):
        return self.totals.get(self._table(sql), 0)


def test_groups_report_true_totals_and_skip_broken():
    pool = _Pool(
        {"unified_contacts": [{"id": "u1", "title": "Аня", "sub": "@ann"}],
         "managed_bots": [{"id": "5", "title": "@shop_bot", "sub": None}]},
        {"unified_contacts": 42, "managed_bots": 1},
        fail=("tg_accounts",))
    d = asyncio.run(fm.find(pool, 7, "ан"))
    by = {g["key"]: g for g in d["groups"]}
    assert set(by) == {"contacts", "bots"}
    assert by["contacts"]["total"] == 42 and by["contacts"]["has_more"] is True
    assert by["bots"]["has_more"] is False and by["bots"]["items"][0]["sub"] == ""
    assert d["total"] == 43


def test_route_rejects_short_query():
    import services.mini_app_api as api
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    orig = api._get_uid
    api._get_uid = lambda request: 7
    try:
        app = web.Application()
        fm.setup_routes(app, pool=None)
        h = next(r.handler for r in app.router.routes()
                 if r.resource.canonical == "/api/miniapp/find" and r.method == "GET")
        resp = asyncio.run(h(make_mocked_request("GET", "/api/miniapp/find?q=a")))
    finally:
        api._get_uid = orig
    assert resp.status == 400


def test_wired_in_api_and_app():
    assert "/api/miniapp/find" in api_routes()
    assert "mini_app_find.setup_routes(app, pool)" in api_source()
    html = miniapp_html()
    assert 'id="moreFindMine"' in html and "findMine(this.value)" in html
    assert '<script src="screens/find_mine.js"></script>' in html
    js = open(os.path.join(ROOT, "mini_app", "screens", "find_mine.js"), encoding="utf-8").read()
    for key in fm.GROUP_KEYS:
        assert re.search(rf"\b{key}\s*:\s*id\s*=>", js), f"группа {key} не открывается"


def test_queries_run_on_real_schema_and_stay_owner_scoped():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    me, other = 88_400_001, 88_400_002

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])", [me, other])
            await pool.execute(
                "INSERT INTO unified_contacts(owner_id, display_name, username) VALUES "
                "($1,'Анна 50% скидка','anna'), ($1,'Борис','boris'), ($2,'Анна чужая','x')", me, other)
            d = await fm.find(pool, me, "анна")
            by = {g["key"]: g for g in d["groups"]}
            assert by["contacts"]["total"] == 1
            assert by["contacts"]["items"][0]["title"] == "Анна 50% скидка"
            d = await fm.find(pool, me, "50%")
            assert {g["key"]: g for g in d["groups"]}["contacts"]["total"] == 1
            d = await fm.find(pool, me, "0_")   # «_» — буква, а не «любой символ»
            assert "contacts" not in {g["key"] for g in d["groups"]}
            # Каждая группа выполняется на настоящей схеме без ошибок.
            for key, _l, rows_sql, count_sql in fm._GROUPS:
                await pool.fetch(rows_sql, me, "%zz%", 5)
                await pool.fetchval(count_sql, me, "%zz%")
        finally:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])", [me, other])
            await pool.close()
    asyncio.run(_go())


def test_deals_are_found_by_title_client_and_note_and_show_the_board_stage():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    me, other = 88_400_011, 88_400_012

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await pool.execute("DELETE FROM crm_deals WHERE owner_id = ANY($1::bigint[])", [me, other])
            await pool.execute(
                "INSERT INTO crm_deals(owner_id, title, contact, stage, notes) VALUES "
                "($1,'Оптовый заказ','Иван','proposal',NULL), "
                "($1,'Подписка','Мария','lead','перезвонить про опт'), "
                "($2,'Оптовый чужой','Пётр','lead',NULL)", me, other)
            d = await fm.find(pool, me, "опт")
            deals = {g["key"]: g for g in d["groups"]}["deals"]
            # Своё по названию и по заметке; чужое не видно.
            assert deals["total"] == 2
            subs = {i["title"]: i["sub"] for i in deals["items"]}
            assert subs["Оптовый заказ"] == "Иван · Предложение"
            d = await fm.find(pool, me, "мари")
            assert {g["key"]: g for g in d["groups"]}["deals"]["items"][0]["title"] == "Подписка"
        finally:
            await pool.execute("DELETE FROM crm_deals WHERE owner_id = ANY($1::bigint[])", [me, other])
            await pool.close()
    asyncio.run(_go())


def test_a_found_deal_opens_its_card_or_says_where_it_is():
    js = open(os.path.join(ROOT, "mini_app", "screens", "find_mine.js"), encoding="utf-8").read()
    body = js[js.index("async function _fmOpenDeal"):]
    body = body[:body.index("\n}\n")]
    assert "await openDeals()" in body and "dkOpenDeal(id)" in body
    assert "toast(" in body, "сделка за потолком доски открывается молча — надо сказать, где она"


def test_bot_find_searches_own_objects_and_links_into_the_app():
    src = open(os.path.join(ROOT, "bot", "handlers", "start.py"), encoding="utf-8").read()
    body = src[src.index("async def cmd_find"):]
    body = body[:body.index("\n@router")]
    # /find ищет тот же набор объектов, что и мини-апп, а не только пункты меню.
    assert "_find_objects(pool" in body
    assert "mini_app_find" in src and "_fm.find(pool, uid" in src
    # Кнопка ведёт прямо в объект, и мини-апп эту ссылку понимает.
    assert "#obj:{g['key']}.{it['id']}" in src
    html = miniapp_html()
    assert "if (k==='obj' && prm) return openFoundObject(String(prm));" in html
    js = open(os.path.join(ROOT, "mini_app", "screens", "find_mine.js"), encoding="utf-8").read()
    assert "function openFoundObject(ref)" in js


def test_found_objects_block_escapes_and_caps_buttons():
    from bot.handlers.start import _found_objects_block
    groups = [{"key": "deals", "label": "💼 Сделки", "total": 12, "has_more": True,
               "items": [{"id": str(i), "title": f"<b>Сделка {i}</b>", "sub": "Иван"} for i in range(10)]}]
    text, buttons = _found_objects_block(groups, "https://x.example/miniapp/")
    assert "&lt;b&gt;Сделка 0&lt;/b&gt;" in text and "(ещё 2)" in text
    assert len(buttons) == 8
    assert buttons[0][1].url == "https://x.example/miniapp/#obj:deals.0"
    text, buttons = _found_objects_block(groups, "")
    assert buttons == [] and "Сделка 9" in text
