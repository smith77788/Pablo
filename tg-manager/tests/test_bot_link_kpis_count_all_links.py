"""Плитки над ссылками бота называют ВСЕ ссылки, а не показанную страницу.

Экран ссылок отдавался с `LIMIT 30` и сам же считал по этим тридцати: плитка
«Ссылок» писала длину страницы, а «Кликов» складывала `click_count` показанных
строк на сервере. У бота с сорока ссылками обе цифры были неправдой, причём
неправдой правдоподобной: число есть, оно просто меньше настоящего. А ссылки
после тридцатой нельзя было ни скопировать, ни удалить — на экране их не было.

Ссылки — рабочий инструмент: по ним считают, откуда пришли люди. Недосчёт
кликов здесь не косметика, он меняет вывод «какой канал работает».
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import mini_app_api as M
from tests.miniapp_source import js_function

UID = 330991
BOT = 7001
PATH = "/api/miniapp/bot/{bot_id}/deeplinks"


class _Pool:
    """Тридцать ссылок на странице, сорок всего, тысяча кликов по всем."""

    def __init__(self, rows: int = 30, total: int = 40, clicks: int = 1000) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows, self.total, self.clicks = rows, total, clicks

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return [{"id": i, "name": f"Ссылка {i}", "start_param": f"p{i}",
                 "click_count": 1, "unique_users": 1, "created_at": None}
                for i in range(self.rows)]

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        if "FROM managed_bots" in q:
            return {"username": "mybot"}
        return {"n": self.total, "clicks": self.clicks}

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return 1 if "managed_bots" in q else 5

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.match_info = {"bot_id": str(BOT)}


def _call(pool, **query) -> dict:
    app = web.Application()
    M.setup_routes(app, pool)
    handler = None
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == "GET" and (info.get("path") or info.get("formatter")) == PATH:
            handler = route.handler
    assert handler, f"роут GET {PATH} не зарегистрирован"
    resp = asyncio.run(handler(_Req(**query)))
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def test_the_clicks_are_summed_by_the_database_over_all_links():
    body = _call(_Pool())
    assert body["total_clicks"] == 1000, (
        "клики снова складываются по показанной странице: "
        f"{body['total_clicks']} вместо тысячи")


def test_the_links_count_is_all_links_not_the_page():
    body = _call(_Pool())
    assert body["total"] == 40, (
        f"число ссылок — это длина страницы ({body.get('total')}), а не все сорок")
    assert body["page"]["has_more"] is True, (
        "за страницей есть ссылки, а ответ об этом молчит")


def test_the_last_page_does_not_promise_more():
    body = _call(_Pool(rows=10, total=10))
    assert body["page"]["has_more"] is False, (
        "ссылки закончились, а кнопка «Показать ещё» останется")


def test_the_offset_reaches_the_links_past_the_page():
    pool = _Pool()
    _call(pool, offset="30")
    page = [(q, a) for q, a in pool.calls if "FROM bot_deep_links" in q
            and "COUNT(" not in q.upper()]
    assert page, "запроса страницы ссылок не было"
    q, args = page[0]
    assert "OFFSET" in q.upper(), "у списка ссылок нет смещения"
    assert 30 in args, f"смещение не передано параметром: {args}"
    assert re.search(r"(?i)order\s+by[^;]*\bid\b", q), (
        "сортировка без добора по id: страницы будут терять и повторять ссылки")


def test_the_screen_shows_the_server_numbers():
    js = js_function("loadDeeplinks")
    assert "totalOf(" in js, "плитка «Ссылок» снова считает длину страницы"
    assert "num(linksTotal)" in js, "число ссылок в плитке не серверное"
    assert "moreBtn(" in js and "offset=" in js, (
        "экран не умеет просить следующую страницу ссылок")
    # Ищется именно ЧТЕНИЕ своего же списка, а не слово в комментарии рядом.
    assert not re.search(r"getElementById\(\s*'dlList'\s*\)\s*\.innerHTML", js), (
        "список снова доклеивается к собственному innerHTML: с «Показать ещё» "
        "так дописывается вторая копия уже показанных ссылок")
