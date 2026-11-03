"""Сделка пятьдесят первая обязана открываться.

Список сделок отдавался с `LIMIT 50` и БЕЗ смещения: ни признака обрезки, ни
способа дойти до остальных. Сто двадцать сделок означали пятьдесят видимых и
семьдесят недостижимых — срез по этапу обрывался на тех же пятидесяти, потому
что потолок стоял и в нём.

Хуже, что экран при этом знал правду и говорил неправду. Чипы над списком
рисуются из `summary`, который считает ВСЮ воронку, и писали «Переговоры 70»
над пятьюдесятью строками, а шапка брала длину загруженной страницы и писала
«50 сделок» при ста двадцати. Тот же класс, что у поиска по контактам:
число описывает один набор, список — другой.

И двух этапов из шести не было в срезах вовсе: чип «Переговоры» показывал
число, а нажать на него было нечего.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import mini_app_api as M

UID = 771204
ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = "/api/miniapp/crm/deals"


class _Pool:
    """Пул, отдающий заданное число сделок и помнящий запросы."""

    def __init__(self, rows: int = 50, total: int = 120) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows = rows
        self.total = total

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        if "GROUP BY stage" in q:
            return [{"stage": "negotiation", "cnt": 70, "total": 0.0}]
        return [{"id": i, "title": f"Сделка {i}", "contact": "", "stage": "lead",
                 "value": None, "notes": None, "created_at": None,
                 "updated_at": None} for i in range(self.rows)]

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return None

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return self.total

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.match_info: dict[str, str] = {}


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


def _page_query(pool) -> tuple[str, tuple]:
    for q, a in pool.calls:
        if "FROM crm_deals" in q and "COUNT(" not in q.upper() and "GROUP BY" not in q:
            return " ".join(q.split()), a
    raise AssertionError("запроса страницы сделок не было")


# ── Ручка ────────────────────────────────────────────────────────────────────

def test_the_offset_reaches_the_fifty_first_deal():
    pool = _Pool()
    _call(pool, offset="50")
    q, args = _page_query(pool)
    assert "OFFSET" in q.upper(), "у списка сделок по-прежнему нет смещения"
    assert 50 in args, f"смещение не передано параметром: {args}"


def test_the_total_is_honest_and_follows_the_filter():
    pool = _Pool()
    body = _call(pool, stage="negotiation")
    assert body.get("total") == 120, (
        f"итог не отдан: шапка снова покажет длину страницы ({body.get('total')})")
    counts = [(q, a) for q, a in pool.calls if "COUNT(" in q.upper()]
    assert counts, "итог не считался запросом"
    cq, ca = counts[0]
    assert "stage=" in cq, "итог считается без среза: шапка соврёт под фильтром"
    assert "negotiation" in ca, f"срез не передан в счётчик параметром: {ca}"


def test_the_answer_says_whether_more_is_left():
    more = _call(_Pool(rows=50, total=120))
    assert more["page"]["has_more"] is True, "продолжение есть, а ответ молчит"
    done = _call(_Pool(rows=20, total=20))
    assert done["page"]["has_more"] is False, (
        "список закончился, а ответ обещает продолжение — кнопка не исчезнет")


def test_the_page_order_is_total():
    """Без добора страницы теряют и повторяют сделки одного дня."""
    q, _ = _page_query_of(_Pool())
    order = re.search(r"(?i)order\s+by\s+(.*?)\s+limit", q)
    assert order, f"в запросе страницы нет ORDER BY: {q[:120]}"
    assert re.search(r"\bid\b", order.group(1)), (
        f"сортировка без добора по id: {order.group(1)}")


def _page_query_of(pool):
    _call(pool)
    return _page_query(pool)


# ── Экран ────────────────────────────────────────────────────────────────────
#
# Своего экрана у простых сделок больше нет: их показывает общая доска сделок
# (screens/deals_board.js) в колонках этапов вместе с воронкой контактов. Эта
# ручка — её «Показать ещё» для простых сделок, и правила те же: число в
# колонке серверное, страница доклеивается по смещению, у каждого этапа своя
# колонка.

def _board_js() -> str:
    return (ROOT / "mini_app" / "screens" / "deals_board.js").read_text(encoding="utf-8")


def test_the_board_counts_deals_by_the_server_total_not_the_page_length():
    from tests.miniapp_source import js_function
    more = js_function("dkMore")
    assert "col.deal_count = dd.total" in more, (
        "колонка доски считает простые сделки по длине загруженной страницы")
    assert "_dkCount(col)" in js_function("_dkRender")


def test_the_board_asks_this_route_for_the_next_page_of_deals():
    from tests.miniapp_source import js_function
    more = js_function("dkMore")
    assert "/api/miniapp/crm/deals?stage=" in more and "&offset=" in more, (
        "доска не умеет просить следующую страницу простых сделок")
    assert "dd.page && dd.page.has_more" in more, (
        "признак продолжения не читается — кнопка не исчезнет в конце списка")
    assert "col.deals.push(" in more, (
        "страница заменяет показанное вместо доклейки: колонка не будет расти")


def test_every_stage_has_a_column_on_the_board():
    """Этап, на котором есть сделки, обязан быть колонкой доски.

    Доска рисует колонки из ответа сервера; сервер берёт их из общего списка
    стадий и ДОБАВЛЯЕТ этап простой сделки, которого в списке нет, — иначе
    такая сделка была бы в итоге и ни в одной колонке.
    """
    src = (ROOT / "services" / "mini_app_deals_board.py").read_text(encoding="utf-8")
    assert "if st not in known" in src
    assert "(_DK.columns || [])" in _board_js() or "_DK.columns || []" in _board_js()
