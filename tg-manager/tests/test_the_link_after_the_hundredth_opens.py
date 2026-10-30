"""Сто первая короткая ссылка не открывалась ничем.

`GET /api/miniapp/links` звал движок без аргументов, а у движка потолок по
умолчанию — сто (`list_for_owner(pool, owner_id, limit=100)`). Экран рисовал
ровно то, что пришло, и ставил рядом с заголовком `items.length` — то есть
длину страницы выдавал за число ссылок. Ни «Показать ещё», ни упоминания
потолка не было.

Цена: ссылка за сотой недостижима из приложения совсем. Её нельзя выключить,
удалить, получить её QR и нельзя увидеть переходы по ней — а переходы и есть то,
зачем ссылку сокращали. Владелец при этом видел честное на вид число «· 100» и
считал, что ссылок у него сто.

Тот же класс, что у семнадцати кнопок «Показать ещё», но с двумя отличиями:
потолок стоял в ЗНАЧЕНИИ ПО УМОЛЧАНИЮ чужой функции, а не в тексте запроса, и
SQL живёт в `services/link_shortener.py` — вне слоя мини-аппа, который
просматривает храповик `test_a_capped_list_says_it_is_capped.py`. Поэтому
правило его и не видело.
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

UID = 515101
PATH = "/api/miniapp/links"


class _Pool:
    """Ссылки владельца: страница по limit/offset и честный итог."""

    def __init__(self, total: int = 260) -> None:
        self.total = total
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        if "short_links" not in q:
            return []
        lim = int(a[1]) if len(a) > 1 else 100
        off = int(a[2]) if len(a) > 2 else 0
        n = max(0, min(lim, self.total - off))
        return [{"code": f"c{off + i:04d}", "target_url": "https://example.com",
                 "title": None, "clicks": off + i, "disabled": False,
                 "created_at": None, "last_click_at": None,
                 "expires_at": None, "tags": None} for i in range(n)]

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        if "COUNT(*)" in q and "short_links" in q:
            return self.total
        return 0

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def execute(self, q, *a):
        self.queries.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers = {"Host": "infragram.test"}
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.match_info: dict[str, str] = {}
        self.scheme = "https"
        self.host = "infragram.test"


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


def _page_query(pool) -> str:
    for q, _ in pool.queries:
        if "short_links" in q and "COUNT(*)" not in q:
            return q
    raise AssertionError("запрос страницы ссылок не найден")


# ── Сервер ─────────────────────────────────────────────────────────────────

def test_the_total_is_not_the_length_of_the_page():
    pool = _Pool(total=260)
    d = _call(pool)

    assert len(d["links"]) == 100, len(d["links"])
    assert d["total"] == 260, (
        "ответ не говорит, сколько ссылок всего: экран снова выдаст длину "
        "страницы за их число")
    assert d["page"]["has_more"] is True, d["page"]


def test_the_page_reaches_past_the_ceiling():
    pool = _Pool(total=260)
    d = _call(pool, limit="100", offset="200")

    assert "OFFSET" in _page_query(pool), _page_query(pool)
    assert [it["code"] for it in d["links"]][:1] == ["c0200"], d["links"][:1]
    assert len(d["links"]) == 60, len(d["links"])
    assert d["page"]["has_more"] is False, d["page"]


def test_the_order_is_total():
    """`created_at` не уникален: без разрыва ничьих страницы теряют ссылки."""
    pool = _Pool()
    _call(pool)
    q = " ".join(_page_query(pool).split())
    m = re.search(r"(?i)order\s+by\s+(.*?)\s+limit", q)
    assert m, q
    order = m.group(1).lower()
    assert "code" in order, (
        f"в порядке страницы нет уникального хвоста: {order}")


def test_a_short_fleet_of_links_says_its_real_number():
    pool = _Pool(total=7)
    d = _call(pool)
    assert d["total"] == 7 and len(d["links"]) == 7
    assert d["page"]["has_more"] is False


# ── Экран ──────────────────────────────────────────────────────────────────

def test_the_screen_asks_for_the_next_page_by_offset():
    js = js_function("lnkLoad")
    assert "offset=" in js, "экран не просит следующую страницу смещением"
    assert "LNK_ROWS" in js and "concat" in js, (
        "страница не доклеивается в тот массив, из которого рисуют строки")
    more = js_function("lnkLoadMore")
    assert "LNK_ROWS.length" in more, (
        "смещение берётся не из числа уже показанных строк")


def test_the_screen_offers_more_and_counts_honestly():
    js = js_function("lnkRenderList")
    assert "moreBtn(" in js and "LNK_TOTAL" in js, (
        "у списка ссылок нет кнопки «Показать ещё» с честным итогом")
    assert "items.length?('· '+items.length)" not in js.replace(" ", ""), (
        "число рядом с заголовком снова длина страницы")


def test_a_reload_starts_the_list_over():
    """Выключение и удаление обновляют список на месте — смещение обязано
    сброситься, иначе после удаления страница начнётся с середины."""
    js = js_function("lnkLoad")
    assert "LNK_OFFSET=0" in js.replace(" ", "") or "LNK_OFFSET = 0" in js, js[:400]
    for name in ("lnkToggle", "lnkDelete"):
        body = js_function(name)
        assert "lnkLoad()" in body, (
            f"{name} обновляет список не свежей загрузкой: смещение останется "
            "от предыдущей страницы")
