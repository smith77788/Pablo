"""Сто первый заказ менеджера не видел никто, включая бухгалтерию.

`GET /api/miniapp/sales/orders` звал движок без аргументов, а у движка потолок
в значении по умолчанию — `list_orders(pool, owner_id, ..., limit=100)`. Экран
рисовал ровно то, что пришло, без итога и без «Показать ещё». Заказы приходят
со временем и сами собой: у работающего бота-продавца сотый — вопрос недель.

Цена выше, чем у списка: заказ — это деньги и живой человек на другом конце.
За сотым заказом нельзя было ни увидеть контакт покупателя, ни отметить статус
«выполнен». А кнопка «⬇️ Экспорт CSV» строила файл из `_spOrdersCache`, то есть
из той же первой страницы: в бухгалтерию уходили первые сто заказов, и файл не
говорил, что он урезан.

Третий случай одной породы (после сто первой короткой ссылки и вставки прокси):
потолок стоит не в тексте обработчика, а в ПОДПИСИ функции, которую он зовёт.
Способ искать такие места — читать подписи вызываемых функций, а не только
запросы в самом обработчике.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import bot_sales_persona as BSP
from services import mini_app_api as M
from services import mini_app_sales as S
from tests.miniapp_source import source_of

UID = 818202
PATH = "/api/miniapp/sales/orders"


class _Pool:
    def __init__(self, total: int = 260) -> None:
        self.total = total
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        if "bot_sales_orders" not in q:
            return []
        lim = int(a[-2]); off = int(a[-1])
        n = max(0, min(lim, self.total - off))
        return [{"id": off + i, "status": "new", "customer_chat_id": 1000 + off + i,
                 "customer_username": None, "customer_name": None,
                 "contact": None, "items": None, "total_cents": 100,
                 "currency": "USD", "promo_code": None, "bot_id": 5,
                 "owner_id": UID} for i in range(n)]

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        if "COUNT(*)" in q and "bot_sales_orders" in q:
            return self.total
        return 0

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def execute(self, q, *a):
        return "OK"


class _Req:
    def __init__(self, **query) -> None:
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.headers = {"Host": "infragram.test"}
        self.match_info: dict[str, str] = {}


def _call(pool, **query) -> dict:
    app = web.Application()
    S.setup_routes(app, pool)
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
    monkeypatch.setattr(S, "_get_uid", lambda r: UID, raising=False)
    yield


def _page_query(pool) -> str:
    for q, _ in pool.queries:
        if "bot_sales_orders" in q and "COUNT(*)" not in q:
            return " ".join(q.split())
    raise AssertionError("запрос страницы заказов не найден")


# ── Сервер ─────────────────────────────────────────────────────────────────

def test_the_answer_says_how_many_orders_there_are():
    pool = _Pool(total=260)
    d = _call(pool)
    assert len(d["orders"]) == 100
    assert d["total"] == 260, (
        "ответ не говорит, сколько заказов всего: экран снова выдаст сотню за "
        "весь список")
    assert d["page"]["has_more"] is True, d["page"]


def test_the_page_reaches_past_the_hundredth():
    pool = _Pool(total=260)
    d = _call(pool, limit="100", offset="200")
    assert "OFFSET" in _page_query(pool), _page_query(pool)
    assert [o["id"] for o in d["orders"]][:1] == [200], d["orders"][:1]
    assert len(d["orders"]) == 60 and d["page"]["has_more"] is False


def test_the_order_has_a_unique_tail():
    """`created_at` у двух заказов совпадает запросто: без `id` в порядке
    соседние страницы теряли бы и повторяли заказы."""
    pool = _Pool()
    _call(pool)
    m = re.search(r"(?i)order\s+by\s+(.*?)\s+limit", _page_query(pool))
    assert m, _page_query(pool)
    assert "id" in m.group(1).lower(), m.group(1)


def test_the_total_obeys_the_same_filters():
    """Итог, посчитанный без фильтра статуса, — это другое число, и «показано
    5 из 260» на отфильтрованном списке врало бы."""
    pool = _Pool(total=260)
    _call(pool, status="done")
    counts = [q for q, _ in pool.queries if "COUNT(*)" in q]
    assert counts and "status=$" in counts[0], counts


def test_a_short_shop_says_its_real_number():
    pool = _Pool(total=4)
    d = _call(pool)
    assert d["total"] == 4 and len(d["orders"]) == 4
    assert d["page"]["has_more"] is False


def test_the_engine_counts_what_the_page_filters():
    pool = _Pool(total=11)
    got = asyncio.run(BSP.count_orders(pool, UID, bot_id=5, status="new"))
    assert got == 11
    q = [q for q, _ in pool.queries if "COUNT(*)" in q][0]
    assert "bot_id=$" in q and "status=$" in q, q


# ── Экран ──────────────────────────────────────────────────────────────────

def _js() -> str:
    return source_of("spOrders")


def test_the_screen_asks_for_the_next_page():
    js = _js()
    assert "offset=' + SP_ORD_OFFSET" in js or "offset='+SP_ORD_OFFSET" in js, (
        "экран не просит следующую страницу смещением")
    assert "SP_ORD_ROWS.concat" in js, (
        "страница не доклеивается в массив, из которого рисуют строки")
    assert "spOrdersMore" in js and "moreBtn(" in js, (
        "у списка заказов нет кнопки «Показать ещё»")


def test_the_screen_names_the_real_number_of_orders():
    js = _js()
    assert "SP_ORD_TOTAL" in js, "экран нигде не берёт честный итог"
    assert "num(SP_ORD_TOTAL)" in js, (
        "итог не выводится: владелец снова решит, что заказов ровно сто")


def test_reopening_the_screen_starts_over():
    js = _js()
    assert "SP_ORD_OFFSET = 0" in js, (
        "смещение не сбрасывается при открытии экрана: список начнётся с "
        "середины")


def test_the_export_does_not_stop_at_the_page():
    js = _js()
    m = re.search(r"async function spExportOrdersCsv\(\)(.*?)\n}\n", js, re.DOTALL)
    assert m, "spExportOrdersCsv больше не асинхронный — он не сможет догрузить"
    body = m.group(1)
    assert "while (all.length < total" in body, (
        "экспорт по-прежнему строится из одной страницы: в бухгалтерию уйдут "
        "первые сто заказов")
    assert "offset=' + all.length" in body, body[:300]
    assert "из " in body and "заказов" in body, (
        "неполный файл не называет, сколько заказов в него не попало")


# ── Тот же потолок в облаке и в реабилитации ───────────────────────────────
#
# `tg_cloud.list_files(..., limit=200)` и `account_rehab.list_for_owner(...,
# limit=100)` — та же порода. У облака цена выше обычного среза: щит
# «⚠️ кусок недоступен» и кнопка ♻️ живут в СТРОКЕ файла, то есть про
# недоступный кусок двести первого файла владелец не узнал бы никак. У
# реабилитации в строке живут ⏩/⏹/🔁: при волне блокировок аккаунт за сотым
# нельзя было ни поторопить, ни остановить, а плитка сверху при этом честно
# писала «Восстанавливаются: 143» над списком из ста.

def test_the_cloud_file_past_the_two_hundredth_is_reachable():
    from services import tg_cloud

    class _CloudPool:
        def __init__(self, total):
            self.total = total

        async def fetch(self, q, *a):
            if "tg_cloud_files" not in q:
                return []
            lim, off = int(a[1]), int(a[2])
            n = max(0, min(lim, self.total - off))
            return [{"id": off + i, "name": f"f{off + i}", "size_bytes": 1,
                     "chunk_count": 1, "sha256": "x", "status": "ok",
                     "created_at": None} for i in range(n)]

        async def fetchval(self, q, *a):
            return self.total if "COUNT(*)" in q else 0

    pool = _CloudPool(260)
    page = asyncio.run(tg_cloud.list_files(pool, UID, limit=200, offset=200))
    assert [f["id"] for f in page][:1] == [200], page[:1]
    assert asyncio.run(tg_cloud.count_files(pool, UID)) == 260


def test_the_cloud_screen_pages_and_counts():
    js = source_of("cloudRefreshList")
    assert "offset=' + CLD_OFFSET" in js, "экран облака не просит смещение"
    assert "CLD_ROWS.concat" in js and "moreBtn(" in js, (
        "у списка файлов нет «Показать ещё»")
    assert "moreBtn(files.length, CLD_TOTAL" in js, (
        "кнопка «Показать ещё» не получает честный итог: её подпись «N из M» "
        "и есть то единственное место, где владелец читает число файлов")


def test_the_rehab_screen_pages_and_counts():
    js = source_of("openRehab")
    assert "offset=' + RHB_OFFSET" in js, "список реабилитации не просит смещение"
    assert "RHB_ROWS.concat" in js and "moreBtn(" in js, (
        "у списка реабилитации нет «Показать ещё»")
    assert "RHB_OFFSET = 0" in js, "смещение не сбрасывается при открытии"
